"""Independent acceptance of terminal failure provenance and bounded requeue.

Pinned MinerU source is patched and executed at its real HTTP and task-manager
hooks. Only external image/HTTP IO is replaced. No parser, model, service,
database or runtime root is used. PostgreSQL budget persistence is covered by
the scratch-only integration companion, not simulated here.
"""

from __future__ import annotations

import ast
import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalCleanupResourceResultV4,
    build_local_cleanup_receipt_v4,
)
from disclosure_anchor.application.ports.remote_parse_v4_failure_committer import (
    V4FinalFailureCommit,
    processing_run_error_from_failure_v4,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderFailedV4,
    RemoteProviderProtocolErrorV4,
    RemoteProviderUnavailableV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import RetryStage
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import (
    TARGET_PREIMAGE_SHA256,
    patch_source,
)
from tests._mineru_admission_fixture import AdmissionFixture
from tests.unit import test_mineru_http_remote_v4 as http_fixture
from tests.unit import test_remote_parse_evidence_v4 as evidence_fixture
from tests.unit import test_staged_coordinator_backend_v4 as backend_fixture


_SERVICE = Path(__file__).resolve().parents[2]
_HTTP_SOURCE = "mineru_vl_utils/vlm_client/http_client.py"
_SAME_MESSAGE = "HTTP 503: unavailable; timeout; CUDA out of memory"
_LEGACY_RECORD_FIELDS = {
    "idempotency_key", "task_id", "attempt_identity", "fence_identity", "state",
    "result_path", "result_sha256", "result_bytes", "result_owner", "lease_until_unix",
    "active_readers", "error", "task_payload", "reserved_result_bytes", "recovery_generation",
    "consumed_at_unix", "cleanup_kind", "ingress_owner",
}
_CAUSE = {
    "schema": "mineru-task-failure-cause.v1", "task_id": "task-1",
    "retry_class": "transient", "code": "vlm_http_status", "http_status": 503,
    "transport_error": None,
}


class _ServerError(RuntimeError):
    pass


def _generated_http_client(transport):
    """Execute the patched pinned class, including its real response checks."""
    raw = (_SERVICE / "tests/fixtures/mineru_344_preimages" / _HTTP_SOURCE).read_bytes()
    if hashlib.sha256(raw).hexdigest() != TARGET_PREIMAGE_SHA256[_HTTP_SOURCE]:
        raise AssertionError("HTTP upstream source identity drifted")
    source = patch_source(_HTTP_SOURCE, raw.decode())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and (node.level or node.module in {"httpx_retries", "loguru"}):
            continue
        nodes.append(node)

    async def image_bytes(image):
        return [image], "png"

    namespace = {
        "VlmClient": object, "ServerError": _ServerError, "RequestError": ValueError,
        "DEFAULT_USER_PROMPT": "", "DEFAULT_SYSTEM_PROMPT": "", "logger": mock.Mock(),
        "aio_image_to_bytes_list_and_format": image_bytes,
        "image_to_bytes_list_and_format": lambda image: ([image], "png"),
    }
    with mock.patch.dict(sys.modules, {"mineru.cli.agent_task_protocol_v2": protocol}):
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<actual-r28-generated-http>", "exec"), namespace)
    client = object.__new__(namespace["HttpVlmClient"])
    client.debug = False
    client.server_url = "http://upstream.invalid"
    client.max_concurrency = 1
    client.system_prompt = ""
    client.allow_truncated_content = False
    client.build_request_body = lambda **values: {"prompt": values["prompt"]}
    client._client = transport

    async def get_transport():
        return transport

    client._aio_client = get_transport
    return client


class _AsyncTransport:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def post(self, _url, *, json):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _failure_receipt(outcome):
    authority = backend_fixture._authority("submitted")
    remote = mock.Mock()
    remote.poll_once.return_value = outcome
    backend, persistence, _, _ = backend_fixture._backend(authority, remote=remote)
    with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability):
        updated = backend.run_remote(
            backend_fixture._work(authority), credit_allowance=ResourceCreditVector(),
            stage_guard=backend_fixture._guard(),
        )
    if updated.state != "cleanup_pending":
        raise AssertionError("accepted terminal failure bypassed cleanup")
    remote.reconcile_or_submit.assert_not_called()
    return persistence.appends[0].new_evidence[0].value


class ProviderTerminalRetryIndependentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = http_fixture.MinerUHttpRemoteV4Tests()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)

    def _poll(self, payload):
        exact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=exact)),
            request_timeout_seconds=30,
        ) as remote:
            outcome = remote.poll_once(self.http._poll_command(self.http._accepted()))
        self.assertIsInstance(outcome, RemoteProviderFailedV4)
        self.assertEqual(outcome.response_sha256, "sha256:" + hashlib.sha256(exact).hexdigest())
        self.assertEqual(outcome.response_byte_count, len(exact))
        return outcome

    async def _generated_failure(self, failure):
        temporary = tempfile.TemporaryDirectory(prefix="independent-terminal-retry-")
        self.addCleanup(temporary.cleanup)
        fixture = AdmissionFixture(Path(temporary.name))
        self.addCleanup(fixture.close)
        self.addAsyncCleanup(fixture.dispose_test_tasks)
        # A fixed task identity lets the actual generated status pass through
        # the existing poll-command fixture without rewriting observed bytes.
        fixture.module.uuid = SimpleNamespace(uuid4=lambda: "task-1")
        # Match the poll fixture's declared epoch, only in this temporary
        # registry. No process or operating-system clock is changed.
        fixture.manager.task_protocol_v2._clock = lambda: 1.0
        options = fixture.options("terminal-cause", fence="fence-1")
        options.agent_idempotency_key = self.http.key
        options.agent_attempt_identity = "attempt-1"

        async def parse_boundary(**_kwargs):
            raise failure

        fixture.module.run_parse_job = parse_boundary
        task = await fixture.create(options)
        queued = fixture.manager.queue.get_nowait()
        self.assertEqual(queued, task.task_id)
        with mock.patch.object(fixture.module.logger, "exception"):
            await fixture.manager._process_task(task.task_id)
        self.assertEqual(fixture.manager.queue._unfinished_tasks, 0)
        cold = fixture.cold_registry()
        record = cold.get(options.agent_idempotency_key)
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.attempt_identity, "attempt-1")
        self.assertEqual(record.fence_identity, "fence-1")
        self.assertEqual(json.loads(record.error), {
            "code": "parse_or_finalize_failed", "detail": type(failure).__name__[:64],
            "schema": "mineru-task-failure.v1",
        })
        return fixture, task, record

    def _cold_status_payload(self, fixture, task):
        fixture.manager.task_protocol_v2 = fixture.cold_registry()
        # Reconstruct the route from durable data as a new API process does.
        fixture.manager.tasks.clear()
        reloaded = fixture.manager.get(task.task_id)

        def route(name, *, task_id):
            suffix = "/result" if name == "get_async_task_result" else ""
            return f"https://mineru.invalid/tasks/{task_id}{suffix}"

        return fixture.manager.build_status_payload(reloaded, SimpleNamespace(url_for=route))

    async def _upstream_payload(self, response, *, secondary=None):
        transport = _AsyncTransport(response)
        client = _generated_http_client(transport)
        try:
            await client.aio_predict(b"synthetic image", "prompt")
        except Exception as exception:
            failure = exception
        else:
            self.fail("the real HTTP boundary silently accepted failure")
        self.assertEqual(transport.calls, 1)
        if secondary is not None:
            secondary(failure)
        fixture, task, record = await self._generated_failure(failure)
        payload = self._cold_status_payload(fixture, task)
        self.assertEqual(payload["error"], record.error)
        return payload

    async def _upstream_outcome(self, response):
        return self._poll(await self._upstream_payload(response))

    async def test_legacy_absent_proof_never_infers_retry_from_diagnostic_words(self):
        for message in (_SAME_MESSAGE, "timeout", "Unexpected status code: [503]", "unknown"):
            with self.subTest(message=message):
                payload = {**self.http._task_payload("failed"), "error": message}
                outcome = self._poll(payload)
                receipt = _failure_receipt(outcome)
                error = processing_run_error_from_failure_v4(receipt)
                self.assertFalse(error["retryable"])
                self.assertEqual(error["retry_budget_class"], "provider_terminal")
                self.assertEqual(error["message"], message)

    async def test_legacy_registry_record_without_new_fields_reloads_conservatively(self):
        fixture, task, record = await self._generated_failure(_ServerError(_SAME_MESSAGE))
        registry_path = fixture.root / ".agent-task-protocol-v2/registry.json"
        persisted = json.loads(registry_path.read_bytes())
        persisted["records"] = [
            {key: value for key, value in item.items() if key in _LEGACY_RECORD_FIELDS}
            for item in persisted["records"]
        ]
        registry_path.write_text(json.dumps(persisted), encoding="utf-8")
        payload = self._cold_status_payload(fixture, task)
        self.assertEqual(payload["error"], record.error)
        failure = _failure_receipt(self._poll(payload))
        self.assertFalse(failure.retryable)
        self.assertEqual(failure.retry_budget_class, "provider_terminal")

    async def test_real_http_content_and_transport_exceptions_reach_task_terminal_reload(self):
        for response in (
            httpx.Response(200, content=b"not JSON"),
            httpx.Response(200, json={"choices": []}),
            httpx.ReadTimeout(_SAME_MESSAGE),
            MemoryError(_SAME_MESSAGE),
        ):
            with self.subTest(family=type(response).__name__):
                transport = _AsyncTransport(response)
                client = _generated_http_client(transport)
                try:
                    await client.aio_predict(b"synthetic image", "prompt")
                except Exception as exception:
                    failure = exception
                else:
                    self.fail("the real HTTP boundary silently accepted failure")
                self.assertEqual(transport.calls, 1)
                fixture, task, record = await self._generated_failure(failure)
                self.assertEqual(fixture.manager.task_protocol_v2.get(record.idempotency_key).error, record.error)
                self.assertEqual(task.status, "failed")

    async def test_allowlisted_upstream_terminal_causes_charge_infrastructure_after_cold_reload(self):
        responses = [httpx.Response(status, content=b"opaque diagnostic") for status in (429, 502, 503, 504)]
        responses += [kind(_SAME_MESSAGE) for kind in (
            httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout,
            httpx.WriteTimeout, httpx.PoolTimeout,
        )]
        for response in responses:
            with self.subTest(cause=getattr(response, "status_code", type(response).__name__)):
                outcome = await self._upstream_outcome(response)
                error = processing_run_error_from_failure_v4(_failure_receipt(outcome))
                self.assertTrue(error["retryable"])
                self.assertEqual(error["retry_budget_class"], "infrastructure")

    async def test_nonallowlisted_status_content_and_resource_failure_never_become_infra_retry(self):
        responses = [httpx.Response(status, content=_SAME_MESSAGE.encode()) for status in (400, 401, 403, 404, 500)]
        responses += [
            httpx.Response(200, content=b"not JSON"), httpx.Response(200, json={"choices": []}),
            httpx.Response(200, json={"object": "error", "message": _SAME_MESSAGE}),
            MemoryError(_SAME_MESSAGE), RuntimeError(_SAME_MESSAGE),
        ]
        responses += [kind(_SAME_MESSAGE) for kind in (
            httpx.ReadError, httpx.WriteError, httpx.CloseError, httpx.RemoteProtocolError,
            httpx.LocalProtocolError, httpx.ProxyError, httpx.UnsupportedProtocol,
        )]
        for response in responses:
            with self.subTest(cause=getattr(response, "status_code", type(response).__name__)):
                outcome = await self._upstream_outcome(response)
                error = processing_run_error_from_failure_v4(_failure_receipt(outcome))
                self.assertFalse(error["retryable"])
                self.assertNotEqual(error["retry_budget_class"], "infrastructure")

    async def test_equal_original_error_strings_have_different_typed_retry_decisions(self):
        transient = await self._upstream_outcome(httpx.Response(503, content=b"opaque diagnostic"))
        content_error = await self._upstream_outcome(httpx.Response(200, json={"choices": []}))
        self.assertEqual(transient.provider_error, content_error.provider_error)
        self.assertTrue(_failure_receipt(transient).retryable)
        self.assertFalse(_failure_receipt(content_error).retryable)

    async def test_failure_cannot_be_grafted_to_other_response_or_remote_task(self):
        genuine = await self._upstream_outcome(httpx.Response(503, content=b"opaque diagnostic"))
        for mutation in (
            {"remote_task_identity": "task-other"},
            {"response_sha256": "sha256:" + "0" * 64},
            {"response_byte_count": genuine.response_byte_count + 1},
        ):
            with self.subTest(mutation=mutation):
                try:
                    grafted = replace(genuine, **mutation)
                    receipt = _failure_receipt(grafted)
                except (ValueError, RemoteProviderProtocolErrorV4):
                    continue
                self.assertFalse(receipt.retryable, "unbound proof cannot authorize retry")

    async def test_actual_terminal_route_refuses_task_attempt_and_fence_grafts(self):
        payload = await self._upstream_payload(httpx.Response(503, content=b"opaque diagnostic"))
        for mutation in (
            {"task_id": "task-other"}, {"attempt_identity": "attempt-other"},
            {"fence_identity": "fence-other"}, {"idempotency_key": "1." + "e" * 64},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(RemoteProviderProtocolErrorV4):
                self._poll({**payload, **mutation})

    async def test_closed_cause_schema_shape_and_derived_class_are_not_provider_assertions(self):
        payload = {**self.http._task_payload("failed"), "error": _SAME_MESSAGE}
        mutations = (
            {"schema": "mineru-task-failure-cause.v2"}, {"code": "timeout"},
            {"retry_class": "retryable"}, {"retry_class": "unknown"},
            {"http_status": True}, {"http_status": 200}, {"http_status": 600},
            {"transport_error": "ReadTimeout"}, {"task_id": "task-other"},
            {"extra": 1}, {"code": "unclassified"},
        )
        malformed = [{**_CAUSE, **mutation} for mutation in mutations]
        malformed += [{key: value for key, value in _CAUSE.items() if key != "http_status"}]
        malformed += [{**_CAUSE, "code": "vlm_transport_error", "http_status": None,
                       "transport_error": "MadeUpTimeout"}]
        for cause in malformed:
            with self.subTest(cause=cause), self.assertRaises(RemoteProviderProtocolErrorV4):
                self._poll({**payload, "failure_cause": cause})

    async def test_cause_is_forbidden_on_nonfailed_task_even_when_shape_is_valid(self):
        for status in ("pending", "processing", "completed"):
            payload = {**self.http._task_payload(status, artifact_bytes=7), "failure_cause": dict(_CAUSE)}
            with self.subTest(status=status), self.assertRaises(RemoteProviderProtocolErrorV4):
                self._poll(payload)

    async def test_absent_and_null_cause_retain_legacy_failure_contract(self):
        payload = {**self.http._task_payload("failed"), "error": _SAME_MESSAGE}
        absent = _failure_receipt(self._poll(payload))
        explicit_null = _failure_receipt(self._poll({**payload, "failure_cause": None}))
        self.assertEqual(absent, explicit_null)
        self.assertFalse(explicit_null.retryable)

    async def test_secondary_cause_or_cleanup_note_revokes_original_transient_proof(self):
        for secondary in (
            lambda failure: failure.add_note("owned image cleanup failed"),
            lambda failure: setattr(failure, "__cause__", RuntimeError("secondary cleanup failure")),
        ):
            with self.subTest(secondary=secondary):
                payload = await self._upstream_payload(
                    httpx.Response(503, content=b"opaque diagnostic"), secondary=secondary,
                )
                self.assertEqual(payload["failure_cause"]["code"], "unclassified")
                self.assertEqual(payload["failure_cause"]["retry_class"], "unknown")
                self.assertFalse(_failure_receipt(self._poll(payload)).retryable)

    async def test_cancelled_untagged_and_custom_transport_subclass_have_no_transient_proof(self):
        class CustomReadTimeout(httpx.ReadTimeout):
            pass

        class Pretender(httpx.ReadTimeout):
            pass

        Pretender.__name__ = "ReadTimeout"
        for subtype in (CustomReadTimeout, Pretender):
            payload = await self._upstream_payload(subtype(_SAME_MESSAGE))
            self.assertEqual(payload["failure_cause"]["retry_class"], "unknown")
            self.assertFalse(_failure_receipt(self._poll(payload)).retryable)
        for error in (asyncio.CancelledError(), RuntimeError(_SAME_MESSAGE), MemoryError(_SAME_MESSAGE)):
            with self.subTest(error=type(error).__name__):
                cause = protocol.task_failure_cause(error, task_id="task-1")
                self.assertEqual(cause["code"], "unclassified")
                self.assertEqual(cause["retry_class"], "unknown")

    async def test_same_transport_exception_without_final_post_origin_remains_unknown(self):
        for error in (httpx.ReadTimeout(_SAME_MESSAGE), httpx.ConnectError(_SAME_MESSAGE)):
            with self.subTest(error=type(error).__name__):
                fixture, task, _ = await self._generated_failure(error)
                payload = self._cold_status_payload(fixture, task)
                self.assertEqual(payload["failure_cause"]["code"], "unclassified")
                self.assertFalse(_failure_receipt(self._poll(payload)).retryable)

    async def test_failed_cleanup_and_ack_keep_old_owner_and_cannot_finalize_new_work(self):
        outcome = await self._upstream_outcome(httpx.Response(503, content=b"opaque diagnostic"))
        authority = backend_fixture._authority("submitted")
        remote = mock.Mock()
        remote.poll_once.return_value = outcome
        backend, persistence, _, materialization = backend_fixture._backend(authority, remote=remote)
        original_append = persistence.append_successor

        def append_and_reload(work, append, *, stage_guard):
            result = original_append(work, append, stage_guard=stage_guard)
            current = persistence.authority
            persistence.authority = replace(
                current, state=append.successor.state,
                lifecycle_version=append.successor.lifecycle_version,
                checkpoint_sha256=append.successor.sha256,
                checkpoint_history=(*current.checkpoint_history, append.successor),
                evidence=(*current.evidence, *append.new_evidence),
            )
            return result

        persistence.append_successor = append_and_reload
        with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability):
            work = backend.run_remote(
                backend_fixture._work(authority), credit_allowance=ResourceCreditVector(),
                stage_guard=backend_fixture._guard(),
            )
        self.assertEqual(work.state, "cleanup_pending")
        failed_append = persistence.appends[-1]
        self.assertTrue(failed_append.new_evidence[0].value.retryable)
        self.assertEqual(failed_append.new_evidence[0].value.retry_budget_class, "infrastructure")
        with self.assertRaises(ValueError):
            V4FinalFailureCommit(
                append=failed_append, failed_at=datetime(2030, 1, 1, tzinfo=UTC),
                outbox_event_id="failure-before-cleanup",
            )

        materialization.cleanup_v4.side_effect = OSError("cleanup not yet complete")
        with self.assertRaisesRegex(OSError, "cleanup not yet complete"):
            backend.cleanup(work, credit_allowance=ResourceCreditVector(), stage_guard=backend_fixture._guard())
        self.assertEqual(len(persistence.appends), 1)
        self.assertEqual(persistence.authority.state, "cleanup_pending")
        materialization.acknowledge_v4.assert_not_called()

        def cleanup_receipt(**values):
            return build_local_cleanup_receipt_v4(
                plan=values["plan"], cleanup_pending_checkpoint=values["checkpoint"],
                results=tuple(LocalCleanupResourceResultV4(
                    kind=resource.kind, relpath=resource.relpath, disposition="absent",
                ) for resource in values["plan"].resources),
            )

        materialization.cleanup_v4.side_effect = cleanup_receipt
        work = backend.cleanup(work, credit_allowance=ResourceCreditVector(), stage_guard=backend_fixture._guard())
        self.assertEqual(work.state, "ack_pending")
        self.assertEqual(work.credits.documents, 1)
        self.assertEqual(work.credits.provider_tasks, 1)
        self.assertEqual(work.credits.ack_items, 1)
        self.assertTrue(persistence.authority.is_current)

        # A fresh backend uses the same durable responsibility after restart.
        # Unavailable ACK must never turn into a new submission or final failure.
        restarted, restarted_persistence, _, restarted_materialization = backend_fixture._backend(
            persistence.authority, remote=remote,
        )
        restarted_materialization.acknowledge_v4.side_effect = RemoteProviderUnavailableV4("ACK unavailable")
        with mock.patch.object(restarted, "_capability", return_value=mock.sentinel.capability):
            with self.assertRaises(RetryStage):
                restarted.acknowledge(work, stage_guard=backend_fixture._guard())
        self.assertEqual(restarted_persistence.appends, [])
        self.assertEqual(restarted_persistence.authority.state, "ack_pending")
        self.assertEqual(restarted_persistence.authority.attempt_id, authority.attempt_id)
        self.assertEqual(restarted_persistence.authority.fence_identity, authority.fence_identity)
        self.assertTrue(restarted_persistence.authority.is_current)
        remote.reconcile_or_submit.assert_not_called()

        def acknowledged_receipt(**values):
            command = values["command"]
            template = evidence_fixture._typed_remote_failure_bundle(provider_result_bytes=0)[2][-1]
            return replace(
                template,
                ack_pending_checkpoint_sha256=command.ack_pending_checkpoint.sha256,
                ack_pending_lifecycle_version=command.ack_pending_checkpoint.lifecycle_version,
                accepted_submission_sha256=command.accepted_submission.sha256,
                failure_receipt_sha256=command.cleanup_plan.failure_receipt_sha256,
                cleanup_plan_sha256=command.cleanup_plan.sha256,
                cleanup_receipt_sha256=command.cleanup_receipt.sha256,
                request_identity=command.request_identity,
                ack_request_sha256=command.ack_request_sha256,
            )

        restarted_materialization.acknowledge_v4.side_effect = acknowledged_receipt
        with mock.patch.object(restarted, "_capability", return_value=mock.sentinel.capability):
            finished = restarted.acknowledge(work, stage_guard=backend_fixture._guard())
        self.assertEqual(finished.state, "remote_failed")
        self.assertEqual(finished.credits, ResourceCreditVector())
        self.assertIsNone(finished.claim_owner_identity)
        self.assertEqual(finished.attempt_id, authority.attempt_id)
        final_commit = V4FinalFailureCommit(
            append=restarted_persistence.appends[-1],
            failed_at=datetime(2030, 1, 1, tzinfo=UTC), outbox_event_id="failure-after-ack",
        )
        self.assertEqual(final_commit.append.successor.state, "remote_failed")
        remote.reconcile_or_submit.assert_not_called()


class TerminalFailureRegistryIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="independent-terminal-registry-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "registry.json"
        self.registry = protocol.DurableTaskRegistry(self.path, max_unacked_result_bytes=1024)
        self.registry.reconcile_or_create(
            idempotency_key="key", task_id="task-1", attempt_identity="attempt-1", fence_identity="fence-1",
        )
        self.error = '{"code":"parse_or_finalize_failed","detail":"ServerError","schema":"mineru-task-failure.v1"}'

    def test_state_error_and_cause_commit_atomically_and_survive_reload(self):
        before = self.registry.get("key")
        before_bytes = self.path.read_bytes()
        with mock.patch.object(self.registry, "_flush_registry_stream", side_effect=OSError("flush refused")):
            with self.assertRaises(protocol.TaskRegistryPersistenceError):
                self.registry.fail("key", error=self.error, failure_cause=dict(_CAUSE))
        self.assertEqual(self.registry.get("key"), before)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        cold = protocol.DurableTaskRegistry(self.path, max_unacked_result_bytes=1024)
        self.assertEqual(cold.get("key"), before)

        original = copy.deepcopy(_CAUSE)
        cold.fail("key", error=self.error, failure_cause=original)
        original["http_status"] = 404
        durable = protocol.DurableTaskRegistry(self.path, max_unacked_result_bytes=1024).get("key")
        self.assertEqual(durable.state, "failed")
        self.assertEqual(durable.error, self.error)
        self.assertEqual(durable.failure_cause, _CAUSE)
        self.assertEqual(cold.get("key"), durable, "caller mutation cannot change durable cause")

    def test_invalid_cause_never_leaves_a_partially_failed_record(self):
        before = self.registry.get("key")
        before_bytes = self.path.read_bytes()
        for mutation in ({"task_id": "other"}, {"schema": "v2"}, {"retry_class": "permanent"}, {"http_status": True}):
            with self.subTest(mutation=mutation), self.assertRaises(protocol.TaskProtocolConflict):
                self.registry.fail("key", error=self.error, failure_cause={**_CAUSE, **mutation})
            self.assertEqual(self.registry.get("key"), before)
            self.assertEqual(self.path.read_bytes(), before_bytes)

    def test_consumed_tombstone_clears_failure_cause_and_cannot_reactivate_task(self):
        self.registry.fail("key", error=self.error, failure_cause=dict(_CAUSE))
        self.registry.acknowledge_failed("key")
        cold = protocol.DurableTaskRegistry(self.path, max_unacked_result_bytes=1024)
        self.assertEqual(cold.get("key").state, "consumed")
        self.assertIsNone(cold.get("key").failure_cause)
        record, created = cold.reconcile_or_create(
            idempotency_key="key", task_id="new-task", attempt_identity="attempt-1", fence_identity="fence-1",
        )
        self.assertFalse(created)
        self.assertEqual(record.task_id, "task-1")
        self.assertEqual(record.state, "consumed")

if __name__ == "__main__":
    unittest.main()
