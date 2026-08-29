from __future__ import annotations

import hashlib
import io
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


class _Reader:
    def read(self, output_dir: Path, *, source_pdf_sha256: str) -> object:
        return {"output": str(output_dir), "source": source_pdf_sha256}

    def locate_artifact_root(self, output_dir: Path) -> Path:
        return output_dir


class MinerUHttpStagedParserTests(unittest.TestCase):
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

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                submissions.append(request.read())
                return httpx.Response(202, json={"task_id": "task-1", "status_url": "/tasks/task-1", "result_url": result_url})
            if request.url.path == "/tasks/task-1":
                return httpx.Response(200, json={"status": "completed"})
            return httpx.Response(200, headers={"content-type": "application/zip"}, content=result)

        source = Path(directory) / "input.pdf"
        source.write_bytes(b"%PDF-stage")
        source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        parser = MinerUHttpStagedParser(api_url="http://mineru.test:30000", server_url="http://vlm.test:30000/v1", spool_root=Path(directory) / "spool", reader=_Reader(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
        return parser, source, source_sha256, submissions

    def test_terminal_receipt_is_resumable_without_downloading_result_body(self) -> None:
        result_gets = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.method == "POST":
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                    },
                )
            if request.url.path == "/tasks/task-1":
                return httpx.Response(200, json={"status": "completed"})
            result_gets += 1
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "content-length": "17",
                },
                content=b"x" * 17,
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
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            handle = parser.begin_remote_parse(
                input_pdf=source,
                options=ParserOptions(),
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
            )
            receipt = handle.wait_terminal()

        self.assertEqual(receipt.artifact_byte_count, 17)
        self.assertEqual(receipt.source_pdf_sha256, source_sha256)
        self.assertNotIn("task-1", repr(receipt))
        resumed = parser.resume_remote_parse(
            receipt=receipt,
            options=ParserOptions(),
        )
        self.assertIsNotNone(resumed)
        self.assertEqual(result_gets, 1)

    def test_submit_rejects_cross_origin_result_url(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={
                    "task_id": "task-1",
                    "status_url": "/tasks/task-1",
                    "result_url": "http://attacker.invalid/result",
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
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "escaped the configured API origin",
            ):
                parser.begin_remote_parse(
                    input_pdf=source,
                    options=ParserOptions(),
                    source_pdf_sha256="sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                    attempt_identity="attempt-1",
                    fence_identity="fence-1",
                )

    def test_effective_defaults_match_pinned_cli_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, submissions = self._completed_parser(
                directory,
                self._zip([("result.txt", b"ok")]),
            )
            parser.begin_remote_parse(input_pdf=source, options=ParserOptions(), source_pdf_sha256=source_sha256, attempt_identity="attempt-1", fence_identity="fence-1")
        body = submissions[0]
        self.assertIn(b'name="effort"\r\n\r\nmedium', body)
        self.assertIn(b'name="image_analysis"\r\n\r\nfalse', body)
        self.assertNotIn(b"none", body.lower())

    def test_same_length_spool_tamper_is_rejected_before_extract(self) -> None:
        result = self._zip([("result.txt", b"one")])
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, _ = self._completed_parser(directory, result)
            handle = parser.begin_remote_parse(input_pdf=source, options=ParserOptions(), source_pdf_sha256=source_sha256, attempt_identity="attempt-1", fence_identity="fence-1")
            receipt = handle.wait_terminal()
            spool = next((Path(directory) / "spool").glob("*.zip"))
            content = bytearray(spool.read_bytes())
            content[-1] ^= 1
            spool.write_bytes(content)
            with self.assertRaisesRegex(ParserOutputContractError, "identity drifted"):
                handle.materialize(receipt=receipt, output_dir=Path(directory) / "out", source_pdf_sha256=source_sha256)
            self.assertFalse((Path(directory) / "out").exists())

    def test_materialize_atomically_promotes_verified_spool(self) -> None:
        result_zip = self._zip([("document/result.txt", b"verified")])
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, _ = self._completed_parser(directory, result_zip)
            handle = parser.begin_remote_parse(input_pdf=source, options=ParserOptions(runtime_bundle_identity_sha256="sha256:" + "a" * 64), source_pdf_sha256=source_sha256, attempt_identity="attempt-1", fence_identity="fence-1")
            receipt = handle.wait_terminal()
            output = Path(directory) / "out"
            parsed = handle.materialize(receipt=receipt, output_dir=output, source_pdf_sha256=source_sha256)
            self.assertEqual(parsed.artifact_root, output)
            self.assertEqual((output / "document" / "result.txt").read_bytes(), b"verified")

    def test_retained_capability_returns_credit_before_result_download(self) -> None:
        result_zip = self._zip([("document/result.txt", b"retained")])
        artifact_sha256 = hashlib.sha256(result_zip).hexdigest()
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{len(result_zip)}".encode()
        ).hexdigest()
        result_gets = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": "task-1", "status_url": "/tasks/task-1", "result_url": "/tasks/task-1/result"})
            if request.url.path == "/tasks/task-1":
                return httpx.Response(200, json={"status": "completed", "result_artifact_schema": "mineru-retained-result.v1", "result_artifact_sha256": artifact_sha256, "result_artifact_bytes": len(result_zip), "result_artifact_owner": owner})
            result_gets += 1
            return httpx.Response(200, headers={"content-type": "application/zip", "x-mineru-result-sha256": artifact_sha256, "x-mineru-result-owner": owner}, content=result_zip)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            parser = MinerUHttpStagedParser(api_url="http://mineru.test:30000", server_url="http://vlm.test:30000/v1", spool_root=Path(directory) / "spool", reader=_Reader(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
            handle = parser.begin_remote_parse(input_pdf=source, options=ParserOptions(runtime_bundle_identity_sha256="sha256:" + "a" * 64), source_pdf_sha256=source_sha256, attempt_identity="attempt-1", fence_identity="fence-1")
            receipt = handle.wait_terminal()
            self.assertEqual(result_gets, 0)
            handle.materialize(receipt=receipt, output_dir=Path(directory) / "out", source_pdf_sha256=source_sha256)
            self.assertEqual(result_gets, 1)

    def test_wait_and_cancel_share_exactly_one_terminal_spool(self) -> None:
        result_zip = self._zip([("result.txt", b"once")])
        result_gets = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": "task-1", "status_url": "/tasks/task-1", "result_url": "/tasks/task-1/result"})
            if request.url.path == "/tasks/task-1":
                return httpx.Response(200, json={"status": "completed"})
            result_gets += 1
            return httpx.Response(200, headers={"content-type": "application/zip"}, content=result_zip)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            parser = MinerUHttpStagedParser(api_url="http://mineru.test:30000", server_url="http://vlm.test:30000/v1", spool_root=Path(directory) / "spool", transport=httpx.MockTransport(handler))
            handle = parser.begin_remote_parse(input_pdf=source, options=ParserOptions(), source_pdf_sha256=source_sha256, attempt_identity="attempt-1", fence_identity="fence-1")
            with ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(handle.wait_terminal)
                drain_future = pool.submit(handle.cancel_and_drain)
                receipt = wait_future.result()
                drain_future.result()
            self.assertGreater(receipt.artifact_byte_count, 0)
            self.assertEqual(result_gets, 1)

    def test_unsafe_zip_fails_without_partial_output(self) -> None:
        for result in (
            self._zip([("../escape", b"bad")]),
            self._zip([("link", b"target")], symlink=True),
        ):
            with self.subTest(kind=result[:12]), tempfile.TemporaryDirectory() as directory:
                parser, source, source_sha256, _ = self._completed_parser(directory, result)
                handle = parser.begin_remote_parse(input_pdf=source, options=ParserOptions(), source_pdf_sha256=source_sha256, attempt_identity="attempt-1", fence_identity="fence-1")
                receipt = handle.wait_terminal()
                output = Path(directory) / "out"
                with self.assertRaisesRegex(ParserOutputContractError, "unsafe ZIP"):
                    handle.materialize(receipt=receipt, output_dir=output, source_pdf_sha256=source_sha256)
                self.assertFalse(output.exists())

    def test_receipt_rejects_non_hex_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            RemoteArtifactReceipt(attempt_identity="a", fence_identity="f", artifact_owner_identity="o", artifact_byte_count=1, source_pdf_sha256="sha256:" + "Z" * 64)


if __name__ == "__main__":
    unittest.main()
