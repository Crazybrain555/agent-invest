from __future__ import annotations

import hashlib
import json
import unittest

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MinerUProtocolV2WireError,
    MinerUResultLeaseExpiredV2,
    api_origin_from_task_routes_v2,
    canonical_client_submit_key_v2,
    canonical_result_owner_v2,
    decode_closed_json_v2,
    parse_result_lease_v2,
    parse_task_payload_v2,
    same_origin_url_v2,
    submission_form_v2,
    submission_request_exact_bytes_v2,
    task_ack_url_v2,
    task_result_url_v2,
    task_status_url_v2,
    validate_absence_payload_v2,
)
from disclosure_anchor.application.ports.parser import ParserOptions


class MinerUProtocolV2WireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api_origin = "https://mineru.invalid"
        self.key = "1." + "a" * 64
        self.attempt = "attempt-1"
        self.fence = "fence-1"

    def test_submission_request_is_exact_and_deterministic(self) -> None:
        options = ParserOptions(
            api_url=self.api_origin,
            server_url="http://127.0.0.1:8000/v1",
            runtime_bundle_identity_sha256="sha256:" + "b" * 64,
        )
        form = submission_form_v2(options, server_url=options.server_url or "")
        exact = submission_request_exact_bytes_v2(
            api_origin=self.api_origin + "/",
            form=form,
            upload_filename="c" * 64 + ".pdf",
        )
        self.assertEqual(
            json.loads(exact),
            {
                "schema": "mineru-staged-request.v1",
                "api_origin": self.api_origin,
                "form": form,
                "upload_filename": "c" * 64 + ".pdf",
            },
        )
        self.assertEqual(
            exact,
            submission_request_exact_bytes_v2(
                api_origin=self.api_origin,
                form=dict(reversed(tuple(form.items()))),
                upload_filename="c" * 64 + ".pdf",
            ),
        )

    def test_client_submit_key_has_frozen_golden_vector(self) -> None:
        self.assertEqual(
            canonical_client_submit_key_v2(
                source_pdf_sha256="sha256:" + "b" * 64,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1,
            ),
            "1.94ae60ed8eaf1371e64c17ff65f7013b3c1e450ebdf8d746104a40fafa4f7df0",
        )
        for epoch in (True, -1, 1.5):
            with self.subTest(epoch=epoch), self.assertRaises(
                MinerUProtocolV2WireError
            ):
                canonical_client_submit_key_v2(
                    source_pdf_sha256="sha256:" + "b" * 64,
                    attempt_identity="attempt-1",
                    fence_identity="fence-1",
                    submission_epoch_unix=epoch,  # type: ignore[arg-type]
                )

    def test_closed_json_rejects_duplicate_nan_extra_and_oversize(self) -> None:
        required = frozenset({"status"})
        allowed = frozenset({"status"})
        for payload in (
            b'{"status":"pending","status":"processing"}',
            b'{"status":NaN}',
            b'{"status":"pending","extra":1}',
            b"x" * (1024 * 1024 + 1),
        ):
            with self.subTest(payload=payload[:40]), self.assertRaises(
                MinerUProtocolV2WireError
            ):
                decode_closed_json_v2(
                    payload,
                    required=required,
                    allowed=allowed,
                )

    def test_absence_payload_is_exact_closed_404_body(self) -> None:
        validate_absence_payload_v2(b'{"detail":"Task not found"}')
        for payload in (
            b'{"detail":"other"}',
            b'{"detail":"Task not found","extra":true}',
        ):
            with self.assertRaises(MinerUProtocolV2WireError):
                validate_absence_payload_v2(payload)

    def test_same_origin_rejects_escape_credentials_query_and_fragment(self) -> None:
        self.assertEqual(
            same_origin_url_v2(
                api_origin=self.api_origin,
                value="/tasks/task-1",
                label="status URL",
            ),
            self.api_origin + "/tasks/task-1",
        )
        for value in (
            "https://other.invalid/tasks/task-1",
            "https://user@mineru.invalid/tasks/task-1",
            "/tasks/task-1?secret=x",
            "/tasks/task-1#fragment",
            "//other.invalid/tasks/task-1",
        ):
            with self.subTest(value=value), self.assertRaises(
                MinerUProtocolV2WireError
            ):
                same_origin_url_v2(
                    api_origin=self.api_origin,
                    value=value,
                    label="status URL",
                )

    def test_task_routes_are_exact_not_merely_same_origin(self) -> None:
        status = task_status_url_v2(
            api_origin=self.api_origin,
            task_id="task/with space",
        )
        result = task_result_url_v2(
            api_origin=self.api_origin,
            task_id="task/with space",
        )
        self.assertEqual(status, self.api_origin + "/tasks/task%2Fwith%20space")
        self.assertEqual(result, status + "/result")
        self.assertEqual(
            task_ack_url_v2(
                api_origin=self.api_origin,
                task_id="task/with space",
            ),
            status + "/ack",
        )
        self.assertEqual(
            api_origin_from_task_routes_v2(
                status_url=status,
                result_url=result,
                task_id="task/with space",
            ),
            self.api_origin,
        )
        for mutated_status, mutated_result in (
            (self.api_origin + "/other/task%2Fwith%20space", result),
            (status, result + "?download=1"),
            (status + "/", result),
        ):
            with self.subTest(
                status=mutated_status, result=mutated_result
            ), self.assertRaises(MinerUProtocolV2WireError):
                api_origin_from_task_routes_v2(
                    status_url=mutated_status,
                    result_url=mutated_result,
                    task_id="task/with space",
                )

    def test_completed_task_requires_canonical_bounded_result(self) -> None:
        payload = self._task_payload(status="completed", artifact_bytes=123)
        observation = parse_task_payload_v2(
            self._exact(payload),
            api_origin=self.api_origin,
            idempotency_key=self.key,
            attempt_identity=self.attempt,
            fence_identity=self.fence,
            artifact_byte_limit=123,
        )
        self.assertEqual(observation.status, "completed")
        self.assertEqual(observation.protocol_state, "completed")
        self.assertEqual(observation.artifact_byte_count, 123)
        for mutation in (
            {"result_artifact_owner": "0" * 64},
            {"result_artifact_bytes": 124},
            {"protocol_state": "processing"},
            {"idempotency_key": "2." + "b" * 64},
        ):
            forged = {**payload, **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(
                MinerUProtocolV2WireError
            ):
                parse_task_payload_v2(
                    self._exact(forged),
                    api_origin=self.api_origin,
                    idempotency_key=self.key,
                    attempt_identity=self.attempt,
                    fence_identity=self.fence,
                    artifact_byte_limit=123,
                )

    def test_noncompleted_task_cannot_carry_result_identity(self) -> None:
        payload = self._task_payload(status="pending")
        payload.update(
            {
                "result_artifact_schema": "mineru-retained-result.v1",
                "result_artifact_sha256": "d" * 64,
                "result_artifact_bytes": 1,
                "result_artifact_owner": canonical_result_owner_v2(
                    task_id="task-1",
                    artifact_sha256="d" * 64,
                    artifact_byte_count=1,
                ),
            }
        )
        with self.assertRaises(MinerUProtocolV2WireError):
            parse_task_payload_v2(
                self._exact(payload),
                api_origin=self.api_origin,
                idempotency_key=self.key,
                attempt_identity=self.attempt,
                fence_identity=self.fence,
            )

    def test_processing_task_allows_provider_finalizing_substate(self) -> None:
        payload = self._task_payload(status="processing")
        payload["protocol_state"] = "finalizing"
        observation = parse_task_payload_v2(
            self._exact(payload),
            api_origin=self.api_origin,
            idempotency_key=self.key,
            attempt_identity=self.attempt,
            fence_identity=self.fence,
        )
        self.assertEqual(observation.status, "processing")
        self.assertEqual(observation.protocol_state, "finalizing")

    def test_optional_task_fields_are_type_closed(self) -> None:
        base = self._task_payload(status="pending")
        mutations: tuple[dict[str, object], ...] = (
            {"backend": 1},
            {"file_names": ["a.pdf", 1]},
            {"queued_ahead": True},
            {"queued_ahead": -1},
            {"created_at": {}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(
                MinerUProtocolV2WireError
            ):
                parse_task_payload_v2(
                    self._exact({**base, **mutation}),
                    api_origin=self.api_origin,
                    idempotency_key=self.key,
                    attempt_identity=self.attempt,
                    fence_identity=self.fence,
                )

    def test_result_lease_rejects_boolean_time_and_extra_fields(self) -> None:
        lease = parse_result_lease_v2(
            b'{"lease_until_unix":123.5,"schema":"mineru-task-protocol.v2",'
            b'"task_id":"task-1"}',
            task_id="task-1",
            observed_at_unix=123.4,
        )
        self.assertEqual(lease.lease_until_unix, 123.5)
        for payload in (
            b'{"lease_until_unix":true,"schema":"mineru-task-protocol.v2",'
            b'"task_id":"task-1"}',
            b'{"lease_until_unix":123,"schema":"mineru-task-protocol.v2",'
            b'"task_id":"task-1","extra":1}',
        ):
            with self.assertRaises(MinerUProtocolV2WireError):
                parse_result_lease_v2(
                    payload,
                    task_id="task-1",
                    observed_at_unix=100.0,
                )

    def test_result_lease_must_be_fresh_when_fully_observed(self) -> None:
        exact = (
            b'{"lease_until_unix":123.5,"schema":"mineru-task-protocol.v2",'
            b'"task_id":"task-1"}'
        )
        for observed in (123.5, 124.0):
            with self.subTest(observed=observed), self.assertRaises(
                MinerUResultLeaseExpiredV2
            ):
                parse_result_lease_v2(
                    exact,
                    task_id="task-1",
                    observed_at_unix=observed,
                )
        for observed in (True, float("nan"), float("inf")):
            with self.subTest(observed=observed), self.assertRaises(
                MinerUProtocolV2WireError
            ):
                parse_result_lease_v2(
                    exact,
                    task_id="task-1",
                    observed_at_unix=observed,
                )

    def _task_payload(self, *, status: str, artifact_bytes: int = 0) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": "task-1",
            "status": status,
            "status_url": "/tasks/task-1",
            "result_url": "/tasks/task-1/result",
            "task_protocol_schema": "mineru-task-protocol.v2",
            "idempotency_key": self.key,
            "attempt_identity": self.attempt,
            "fence_identity": self.fence,
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

    @staticmethod
    def _exact(payload: dict[str, object]) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


if __name__ == "__main__":
    unittest.main()
