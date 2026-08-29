from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    MinerUHttpStagedParser,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import ParserOutputContractError


class MinerUHttpStagedParserTests(unittest.TestCase):
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

        parser = MinerUHttpStagedParser(
            api_url="http://mineru.test:30000",
            server_url="http://vlm.test:30000/v1",
            transport=httpx.MockTransport(handler),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
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

        parser = MinerUHttpStagedParser(
            api_url="http://mineru.test:30000",
            server_url="http://vlm.test:30000/v1",
            transport=httpx.MockTransport(handler),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "escaped the configured API origin",
            ):
                parser.begin_remote_parse(
                    input_pdf=source,
                    options=ParserOptions(),
                    source_pdf_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    attempt_identity="attempt-1",
                    fence_identity="fence-1",
                )


if __name__ == "__main__":
    unittest.main()
