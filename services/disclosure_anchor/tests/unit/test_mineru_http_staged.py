from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import io
import json
import stat
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    MinerUHttpStagedParser,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.staged_provider_parser import (
    RemoteArtifactReceipt,
)
from disclosure_anchor.domain.errors import ParserOutputContractError

PINNED_OPTIONS = ParserOptions(runtime_bundle_identity_sha256="sha256:" + "a" * 64)


def _expected_idempotency_key(
    source_sha256: str, attempt: str, fence: str, epoch: int
) -> str:
    digest = hashlib.sha256(
        f"{epoch:x}\0{source_sha256}\0{attempt}\0{fence}".encode()
    ).hexdigest()
    return f"{epoch:x}.{digest}"


class _Reader:
    def read(self, output_dir: Path, *, source_pdf_sha256: str) -> object:
        return {"output": str(output_dir), "source": source_pdf_sha256}

    def locate_artifact_root(self, output_dir: Path) -> Path:
        return output_dir


class MinerUHttpStagedParserTests(unittest.TestCase):
    def test_resume_rejects_retired_v1_and_v2_tokens(self) -> None:
        parser = MinerUHttpStagedParser(
            api_url="http://mineru.test:30000",
            server_url="http://vlm.test:30000/v1",
            spool_root=Path("/unused"),
        )
        receipt = RemoteArtifactReceipt(
            attempt_identity="attempt-1",
            fence_identity="fence-1",
            artifact_owner_identity="owner-1",
            artifact_byte_count=1,
            artifact_sha256="a" * 64,
            source_pdf_sha256="sha256:" + "b" * 64,
        )
        for version in (1, 2):
            with self.subTest(version=version):
                token = base64.urlsafe_b64encode(
                    json.dumps(
                        {"v": version}, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).decode()
                with self.assertRaisesRegex(
                    ParserOutputContractError, "resume token shape"
                ):
                    parser.resume_remote_parse(
                        receipt=replace(receipt, resume_token=token),
                        options=PINNED_OPTIONS,
                    )

    def test_failed_remote_ack_requires_failure_committed_checkpoint(self) -> None:
        ack_calls = 0
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal ack_calls
                if request.url.path.endswith("/ack"):
                    ack_calls += 1
                    return httpx.Response(
                        200,
                        json={
                            "schema": "mineru-task-protocol.v2",
                            "task_id": "task-1",
                            "status": "consumed",
                        },
                    )
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )

            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                input_pdf=source,
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "failure_committed"):
                handle.acknowledge_after_failure_committed(
                    checkpoint_state="remote_failed"
                )
            self.assertEqual(ack_calls, 0)
            handle.acknowledge_after_failure_committed(
                checkpoint_state="failure_committed"
            )
            self.assertEqual(ack_calls, 1)

    @staticmethod
    def _zip(entries: list[tuple[str, bytes]], *, symlink: bool = False) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in entries:
                if symlink:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, content)
                else:
                    archive.writestr(name, content)
        return buffer.getvalue()

    def _completed_parser(
        self,
        directory: str,
        result: bytes,
        *,
        result_url: str = "/tasks/task-1/result",
    ) -> tuple[MinerUHttpStagedParser, Path, str, list[bytes]]:
        submissions: list[bytes] = []
        artifact_sha256 = hashlib.sha256(result).hexdigest()
        artifact_bytes = len(result)
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{artifact_bytes}".encode()
        ).hexdigest()
        idempotency_key = ""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                submissions.append(request.read())
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": result_url,
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "POST" and request.url.path.endswith("/lease"):
                return httpx.Response(
                    200,
                    json={
                        "schema": "mineru-task-protocol.v2",
                        "task_id": "task-1",
                        "lease_until_unix": 9_999_999_999.0,
                    },
                )
            if request.url.path == "/tasks/task-1":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "protocol_state": "completed",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                        "result_artifact_schema": "mineru-retained-result.v1",
                        "result_artifact_sha256": artifact_sha256,
                        "result_artifact_bytes": artifact_bytes,
                        "result_artifact_owner": owner,
                    },
                )
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "x-mineru-result-sha256": artifact_sha256,
                    "x-mineru-result-owner": owner,
                },
                content=result,
            )

        source = Path(directory) / "input.pdf"
        source.write_bytes(b"%PDF-stage")
        source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        idempotency_key = _expected_idempotency_key(
            source_sha256, "attempt-1", "fence-1", 1_000_000
        )
        parser = MinerUHttpStagedParser(
            api_url="http://mineru.test:30000",
            server_url="http://vlm.test:30000/v1",
            spool_root=Path(directory) / "spool",
            reader=_Reader(),
            transport=httpx.MockTransport(handler),
        )  # type: ignore[arg-type]
        return parser, source, source_sha256, submissions

    def test_v2_submission_binds_identities_and_leases_before_credit_return(
        self,
    ) -> None:
        calls: list[str] = []
        artifact_sha256 = "a" * 64
        artifact_bytes = 17
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{artifact_bytes}".encode()
        ).hexdigest()

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "POST" and request.url.path == "/tasks":
                body = request.read()
                self.assertIn(b"agent_idempotency_key", body)
                self.assertIn(b"attempt-1", body)
                self.assertIn(b"fence-1", body)
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "protocol_state": "completed",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                        "result_artifact_schema": "mineru-retained-result.v1",
                        "result_artifact_sha256": artifact_sha256,
                        "result_artifact_bytes": artifact_bytes,
                        "result_artifact_owner": owner,
                    },
                )
            if request.url.path.endswith("/ack"):
                return httpx.Response(
                    200,
                    json={
                        "schema": "mineru-task-protocol.v2",
                        "task_id": "task-1",
                        "status": "consumed",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "schema": "mineru-task-protocol.v2",
                    "task_id": "task-1",
                    "lease_until_unix": 9999999999.0,
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            idempotency_key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                input_pdf=source,
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            receipt = handle.wait_terminal()
            with self.assertRaisesRegex(
                ParserOutputContractError, "finish_committed"
            ):
                handle.acknowledge_after_finish_committed(
                    receipt=receipt, checkpoint_state="local_materialized"
                )
            handle.acknowledge_after_finish_committed(
                receipt=receipt, checkpoint_state="finish_committed"
            )
            handle.acknowledge_after_finish_committed(
                receipt=receipt, checkpoint_state="finish_committed"
            )
        self.assertEqual(receipt.artifact_sha256, artifact_sha256)
        self.assertEqual(calls.count("POST /tasks/task-1/ack"), 2)

    def test_v2_rejects_duplicate_submit_wire_fields(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                content=(
                    b'{"task_id":"task-1","task_id":"task-2",'
                    b'"status_url":"/tasks/task-1",'
                    b'"result_url":"/tasks/task-1/result"}'
                ),
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaisesRegex(ParserOutputContractError, "duplicate"):
                parser.begin_remote_parse(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    source_pdf_sha256="sha256:"
                    + hashlib.sha256(source.read_bytes()).hexdigest(),
                    attempt_identity="attempt-1",
                    fence_identity="fence-1",
                    submission_epoch_unix=1_000_000,
                )

    def test_v2_accepts_identity_bound_existing_post_200(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )

            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "task_id": "task-1", "status_url": "/tasks/task-1",
                    "result_url": "/tasks/task-1/result",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "idempotency_key": key, "attempt_identity": "attempt-1",
                    "fence_identity": "fence-1",
                })

            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                input_pdf=source, options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1", fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            self.assertIsNotNone(handle)

    def test_v2_rejects_wire_json_before_exceeding_bound(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202, content=b"{" + b" " * (1024 * 1024) + b"}",
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaisesRegex(ParserOutputContractError, "wire envelope"):
                parser.begin_remote_parse(
                    input_pdf=source, options=PINNED_OPTIONS,
                    source_pdf_sha256="sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                    attempt_identity="attempt-1", fence_identity="fence-1",
                    submission_epoch_unix=1_000_000,
                )

    def test_submit_rejects_cross_origin_result_url(self) -> None:
        idempotency_key = ""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={
                    "task_id": "task-1",
                    "status_url": "/tasks/task-1",
                    "result_url": "http://attacker.invalid/result",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "idempotency_key": idempotency_key,
                    "attempt_identity": "attempt-1",
                    "fence_identity": "fence-1",
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            idempotency_key = _expected_idempotency_key(
                "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                "attempt-1",
                "fence-1",
                1_000_000,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "escaped the configured API origin",
            ):
                parser.begin_remote_parse(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    source_pdf_sha256="sha256:"
                    + hashlib.sha256(source.read_bytes()).hexdigest(),
                    attempt_identity="attempt-1",
                    fence_identity="fence-1",
                    submission_epoch_unix=1_000_000,
                )

    def test_effective_defaults_match_pinned_cli_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, submissions = self._completed_parser(
                directory,
                self._zip([("result.txt", b"ok")]),
            )
            parser.begin_remote_parse(
                input_pdf=source,
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
        body = submissions[0]
        self.assertIn(b'name="effort"\r\n\r\nmedium', body)
        self.assertIn(b'name="image_analysis"\r\n\r\nfalse', body)
        self.assertNotIn(b"none", body.lower())

    def test_materialize_atomically_promotes_verified_spool(self) -> None:
        result_zip = self._zip([("document/result.txt", b"verified")])
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, _ = self._completed_parser(
                directory, result_zip
            )
            handle = parser.begin_remote_parse(
                input_pdf=source,
                options=ParserOptions(
                    runtime_bundle_identity_sha256="sha256:" + "a" * 64
                ),
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            receipt = handle.wait_terminal()
            output = Path(directory) / "out"
            parsed = handle.materialize(
                receipt=receipt, output_dir=output, source_pdf_sha256=source_sha256
            )
            self.assertEqual(parsed.artifact_root, output)
            self.assertEqual(
                (output / "document" / "result.txt").read_bytes(), b"verified"
            )

    def test_retained_capability_returns_credit_before_result_download(self) -> None:
        result_zip = self._zip([("document/result.txt", b"retained")])
        artifact_sha256 = hashlib.sha256(result_zip).hexdigest()
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{len(result_zip)}".encode()
        ).hexdigest()
        result_gets = 0
        idempotency_key = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "POST" and request.url.path.endswith("/lease"):
                return httpx.Response(200, json={
                    "schema": "mineru-task-protocol.v2", "task_id": "task-1",
                    "lease_until_unix": 9_999_999_999.0,
                })
            if request.url.path == "/tasks/task-1":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "protocol_state": "completed",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                        "result_artifact_schema": "mineru-retained-result.v1",
                        "result_artifact_sha256": artifact_sha256,
                        "result_artifact_bytes": len(result_zip),
                        "result_artifact_owner": owner,
                    },
                )
            result_gets += 1
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "x-mineru-result-sha256": artifact_sha256,
                    "x-mineru-result-owner": owner,
                },
                content=result_zip,
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            idempotency_key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                reader=_Reader(),
                transport=httpx.MockTransport(handler),
            )  # type: ignore[arg-type]
            handle = parser.begin_remote_parse(
                input_pdf=source,
                options=ParserOptions(
                    runtime_bundle_identity_sha256="sha256:" + "a" * 64
                ),
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            receipt = handle.wait_terminal()
            self.assertEqual(result_gets, 0)
            handle.materialize(
                receipt=receipt,
                output_dir=Path(directory) / "out",
                source_pdf_sha256=source_sha256,
            )
            self.assertEqual(result_gets, 1)

    def test_wait_and_cancel_share_exactly_one_retained_receipt(self) -> None:
        result_zip = self._zip([("result.txt", b"once")])
        result_gets = 0
        idempotency_key = ""
        artifact_sha256 = hashlib.sha256(result_zip).hexdigest()
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{len(result_zip)}".encode()
        ).hexdigest()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "POST" and request.url.path.endswith("/lease"):
                return httpx.Response(200, json={
                    "schema": "mineru-task-protocol.v2", "task_id": "task-1",
                    "lease_until_unix": 9_999_999_999.0,
                })
            if request.url.path == "/tasks/task-1":
                return httpx.Response(200, json={
                    "status": "completed",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "protocol_state": "completed",
                    "idempotency_key": idempotency_key,
                    "attempt_identity": "attempt-1",
                    "fence_identity": "fence-1",
                    "result_artifact_schema": "mineru-retained-result.v1",
                    "result_artifact_sha256": artifact_sha256,
                    "result_artifact_bytes": len(result_zip),
                    "result_artifact_owner": owner,
                })
            result_gets += 1
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "x-mineru-result-sha256": artifact_sha256,
                    "x-mineru-result-owner": owner,
                },
                content=result_zip,
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            idempotency_key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                input_pdf=source,
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(handle.wait_terminal)
                drain_future = pool.submit(handle.cancel_and_drain)
                receipt = wait_future.result()
                drain_future.result()
            self.assertGreater(receipt.artifact_byte_count, 0)
            self.assertEqual(result_gets, 0)

    def test_unsafe_zip_fails_without_partial_output(self) -> None:
        for result in (
            self._zip([("../escape", b"bad")]),
            self._zip([("link", b"target")], symlink=True),
        ):
            with (
                self.subTest(kind=result[:12]),
                tempfile.TemporaryDirectory() as directory,
            ):
                parser, source, source_sha256, _ = self._completed_parser(
                    directory, result
                )
                handle = parser.begin_remote_parse(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    source_pdf_sha256=source_sha256,
                    attempt_identity="attempt-1",
                    fence_identity="fence-1",
                    submission_epoch_unix=1_000_000,
                )
                receipt = handle.wait_terminal()
                output = Path(directory) / "out"
                with self.assertRaisesRegex(ParserOutputContractError, "unsafe ZIP"):
                    handle.materialize(
                        receipt=receipt,
                        output_dir=output,
                        source_pdf_sha256=source_sha256,
                    )
                self.assertFalse(output.exists())

    def test_receipt_rejects_non_hex_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            RemoteArtifactReceipt(
                attempt_identity="a",
                fence_identity="f",
                artifact_owner_identity="o",
                artifact_byte_count=1,
                source_pdf_sha256="sha256:" + "Z" * 64,
            )


if __name__ == "__main__":
    unittest.main()
