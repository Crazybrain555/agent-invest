from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import date
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.quick_pdf_text import (
    QuickPdfTextChunk,
    QuickPdfTextError,
    QuickPdfTextResult,
)
from disclosure_anchor.application.ports.processing_requests import OriginalLocator
from disclosure_anchor.application.use_cases.processing_requests import (
    ProcessingRequestPolicy,
    ProcessingRequestService,
)
from disclosure_anchor.application.use_cases.read_quick_document_text import (
    ReadQuickDocumentText,
)
from disclosure_anchor.domain.errors import ConfigurationError
from disclosure_anchor.settings import Settings

from tests.unit._env import without_db_env
from tests.unit.processing_request_fakes import (
    ProcessingRequestStore,
    StoreStatusReader,
    local_document,
    uow_factory,
)

try:
    from fastapi.testclient import TestClient

    from disclosure_anchor.agent_api import create_agent_api_app
    from disclosure_anchor.api.agent_middleware import AgentApiGuard
    from disclosure_anchor.api.routers.document_text import QuickTextReader
except ModuleNotFoundError:  # pragma: no cover - API extras absent
    TestClient = None  # type: ignore[assignment, misc]

READ = "r" * 40
REQUEST = "q" * 40
DOC = "doc_01M4B45TP76AY533REBBYHCBGX"
HASH = "sha256:" + "a" * 64
KEY = "agent-key-aaaaaaaaaaaaaaaa"


def _settings(root: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "disclosure_data_root": root / "services" / "disclosure_anchor",
        "disclosure_shared_root": root / "shared",
        "disclosure_runtime_root": root / "services" / "disclosure_anchor" / "runtime",
        "mineru_model_cache": root / "shared" / "mineru",
        "hf_home": root / "shared" / "hf",
        "modelscope_cache": root / "shared" / "modelscope",
        "database_url": None,
        "disclosure_agent_api_read_token": READ,
        "disclosure_agent_api_request_token": REQUEST,
        "disclosure_agent_api_allowed_hosts": "testserver,127.0.0.1:8712",
    }
    values.update(overrides)
    return Settings(**values)


class _Locator:
    def __init__(self, located: OriginalLocator | None) -> None:
        self.located = located

    def locate(self, document_id: str) -> OriginalLocator | None:
        return self.located


@dataclass
class _Extractor:
    error: QuickPdfTextError | None = None
    calls: list[dict[str, Any]] | None = None
    gate: threading.Event | None = None

    def extract(self, **kwargs: Any) -> QuickPdfTextResult:
        if self.calls is not None:
            self.calls.append(kwargs)
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return QuickPdfTextResult(
            source_sha256=HASH, source_byte_count=1234, extractor="pdfium_native_text",
            extractor_version="pypdfium2-5.13.0", total_pages=2,
            chunks=(QuickPdfTextChunk(page_number=1, char_start=0, char_end=5, page_text_chars=5,
                                      text="甲乙丙丁戊", page_complete=True, no_extractable_text=False),
                    QuickPdfTextChunk(page_number=2, char_start=0, char_end=0, page_text_chars=0,
                                      text="", page_complete=True, no_extractable_text=True)),
            next_cursor=None, complete=True, full_document_traversed=True, pages_scanned=2,
            next_page=3, next_char_offset=0, limitations=("native text only",),
        )


def _located(**overrides: Any) -> OriginalLocator:
    values: dict[str, Any] = {
        "document_id": DOC, "status": "registered", "raw_file_hash": HASH,
        "raw_file_relpath": "raw_documents/cninfo/600519/2026/1220000001/sha256_" + "a" * 64 + ".pdf",
        "provider": "cninfo", "provider_document_id": "1220000001", "security_code": "600519",
        "exchange": "SSE", "title": "2025年年度报告", "announcement_date": date(2026, 3, 30),
        "source_ref": "asset://disclosure/source/x", "company_ref": None, "security_ref": None,
    }
    values.update(overrides)
    return OriginalLocator(**values)


@unittest.skipIf(TestClient is None, "fastapi test client is not installed")
class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = without_db_env()
        self.env.start()
        self.store = ProcessingRequestStore(clock=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc))
        self.store.documents[DOC] = local_document(DOC)
        self.service = ProcessingRequestService(
            uow_factory=uow_factory(self.store),
            status_reader=StoreStatusReader(self.store),
            policy=ProcessingRequestPolicy(daily_request_limit=50),
            clock=self.store.clock,
        )
        self.extractor = _Extractor(calls=[])
        self.locator = _Locator(_located())
        self.slots = threading.BoundedSemaphore(1)
        self.settings = _settings(self.root)
        self.app = create_agent_api_app(
            self.settings,
            validate_runtime=False,
            processing_requests=self.service,
            quick_text=QuickTextReader(
                use_case=ReadQuickDocumentText(
                    locator=self.locator, extractor=self.extractor,
                    original_root=self.settings.disclosure_data_root / "data",
                ),
                slots=self.slots,
            ),
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.env.stop()
        self._tmp.cleanup()

    def _post(self, body: dict[str, Any], token: str = REQUEST, **headers: str) -> Any:
        return self.client.post("/v1/processing-requests", json=body,
                                headers={"Authorization": f"Bearer {token}", **headers})

    def _get(self, path: str, token: str = READ, **headers: str) -> Any:
        return self.client.get(path, headers={"Authorization": f"Bearer {token}", **headers})

    def test_startup_fails_closed_on_missing_equal_or_short_tokens_and_remote_host(self) -> None:
        for overrides in (
            {"disclosure_agent_api_request_token": None},
            {"disclosure_agent_api_request_token": READ},
            {"disclosure_agent_api_read_token": "short"},
            {"disclosure_agent_api_host": "0.0.0.0"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ConfigurationError):
                create_agent_api_app(_settings(self.root, **overrides), validate_runtime=False)

    def test_scoped_tokens_and_transport_guards(self) -> None:
        body = {"idempotency_key": KEY, "target": {"kind": "document", "document_id": DOC}}
        cases = [
            (self.client.post("/v1/processing-requests", json=body), 401, "UNAUTHORIZED"),
            (self._post(body, token="x" * 40), 401, "UNAUTHORIZED"),
            (self._post(body, token=READ), 403, "FORBIDDEN"),
            (self._get("/v1/documents", token=REQUEST), 403, "FORBIDDEN"),
            (self._get("/v1/documents", Origin="https://evil.invalid"), 403, "FORBIDDEN"),
            (self._get("/v1/documents", Host="evil.invalid"), 403, "FORBIDDEN"),
        ]
        for response, status_code, error_code in cases:
            with self.subTest(status=status_code, code=error_code):
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json()["error_code"], error_code)
        # Verbatim read routers sit behind the read token too.
        self.assertEqual(self.client.get("/v1/documents/" + DOC + "/units").status_code, 401)

    def test_inventory_and_download_link_are_mounted_behind_the_read_token(self) -> None:
        for path in ("/v1/disclosure-inventory", f"/v1/documents/{DOC}/download-link"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self._get(path, token=REQUEST).status_code, 403)
        # Without a database the inventory reader is absent and says so.
        unavailable = self._get("/v1/disclosure-inventory")
        self.assertEqual((unavailable.status_code, unavailable.json()["error_code"]),
                         (503, "SERVICE_UNAVAILABLE"))

    def test_guard_refuses_remote_peers_duplicates_and_unbounded_posts(self) -> None:
        access = self.app.state.agent_api_access

        async def call(scope_overrides: dict[str, Any], headers: list[tuple[bytes, bytes]]) -> int:
            sent: list[dict[str, Any]] = []

            async def app(scope: Any, receive: Any, send: Any) -> None:
                raise AssertionError("refused requests never reach the app")

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                sent.append(message)

            scope = {"type": "http", "method": "GET", "path": "/v1/documents",
                     "headers": headers, "client": ("127.0.0.1", 5000), **scope_overrides}
            await AgentApiGuard(app, access=access)(scope, receive, send)
            return int(sent[0]["status"])

        auth = (b"authorization", f"Bearer {READ}".encode())
        host = (b"host", b"testserver")
        self.assertEqual(asyncio.run(call({"client": ("10.0.0.5", 5000)}, [auth, host])), 403)
        self.assertEqual(asyncio.run(call({}, [auth, auth, host])), 401)
        self.assertEqual(asyncio.run(call({}, [auth, host, host])), 403)
        post_auth = (b"authorization", f"Bearer {REQUEST}".encode())
        self.assertEqual(asyncio.run(call({"method": "POST"}, [post_auth, host])), 411)
        self.assertEqual(
            asyncio.run(call({"method": "POST"}, [post_auth, host, (b"content-length", b"999999")])),
            413,
        )

    def test_post_accepts_promptly_then_replays_and_conflicts(self) -> None:
        body = {"idempotency_key": KEY, "target": {
            "kind": "title_match", "security_code": "600519", "exchange": "SSE",
            "date_from": "2026-01-01", "date_to": "2026-03-31", "title_contains": "年度报告"}}
        created = self._post(body)
        self.assertEqual(created.status_code, 201)
        payload = created.json()
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["status"]["state"], "resolving")
        self.assertEqual(payload["status"]["next_action"], "wait")
        replay = self._post(body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["request_id"], payload["request_id"])
        self.assertTrue(replay.json()["replayed"])
        conflict = self._post({**body, "enqueue_parse": False})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error_code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(conflict.json()["detail"]["request_id"], payload["request_id"])
        status = self._get(f"/v1/processing-requests/{payload['request_id']}")
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("replayed", status.json())
        self.assertEqual(status.json()["status"]["status_sha256"], payload["status"]["status_sha256"])

    def test_validation_and_unknown_requests(self) -> None:
        bad_window = {"idempotency_key": KEY, "target": {
            "kind": "title_match", "security_code": "600519", "exchange": "SSE",
            "date_from": "2024-01-01", "date_to": "2026-01-01", "title_contains": "年度报告"}}
        response = self._post(bad_window)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["errors"][0]["field"], "target.date_to")
        unknown_kind = self._post({"idempotency_key": KEY, "target": {"kind": "url", "url": "x"}})
        self.assertEqual(unknown_kind.status_code, 422)
        missing_doc = self._post({"idempotency_key": KEY, "target": {
            "kind": "document", "document_id": "doc_01M4B45TP76AY533REBBYHCBG0"}})
        self.assertEqual(missing_doc.status_code, 404)
        self.assertEqual(self._get("/v1/processing-requests/dreq_" + "0" * 26).status_code, 404)
        self.assertEqual(self._get("/v1/processing-requests/../../etc").status_code, 404)
        self.assertEqual(self._get("/v1/processing-requests/dreq_x?extra=1").status_code, 422)

    def test_wait_is_bounded_and_returns_terminal_immediately(self) -> None:
        created = self._post({"idempotency_key": KEY, "enqueue_parse": False,
                              "target": {"kind": "document", "document_id": DOC}}).json()
        self.assertTrue(created["status"]["terminal"])
        waited = self._get(f"/v1/processing-requests/{created['request_id']}/wait?timeout_s=20")
        self.assertEqual(waited.status_code, 200)
        self.assertEqual(waited.json()["wait"]["outcome"], "terminal")
        for query in ("timeout_s=0", "timeout_s=21", "since=sha256:zz"):
            with self.subTest(query=query):
                response = self._get(f"/v1/processing-requests/{created['request_id']}/wait?{query}")
                self.assertEqual(response.status_code, 422)

    def test_text_reads_trusted_locator_and_reports_honest_coverage(self) -> None:
        response = self._get(f"/v1/documents/{DOC}/text?max_chars=100&max_pages=2")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["raw_file_hash"], HASH)
        self.assertFalse(body["formal_units_available"])
        self.assertTrue(body["chunks"][1]["no_extractable_text"])
        self.assertEqual(body["citation"]["text_kind"], "quick_native_pdf_text")
        self.assertNotIn("raw_file_relpath", json.dumps(body))
        call = self.extractor.calls[-1]  # type: ignore[index]
        self.assertEqual(call["root"], self.settings.disclosure_data_root / "data")
        self.assertEqual(str(call["relpath"]), _located().raw_file_relpath)
        self.assertEqual(call["expected_sha256"], HASH)

    def test_text_errors_map_to_typed_envelopes(self) -> None:
        cases = [
            ("invalid_cursor", 422, "TEXT_INVALID_CURSOR"),
            ("cursor_source_mismatch", 409, "TEXT_CURSOR_SOURCE_MISMATCH"),
            ("source_hash_mismatch", 500, "ORIGINAL_INTEGRITY_ERROR"),
            ("source_not_found", 500, "ORIGINAL_INTEGRITY_ERROR"),
            ("source_unavailable", 503, "ORIGINAL_UNAVAILABLE"),
            ("password_required", 409, "TEXT_SOURCE_UNSUPPORTED"),
            ("extraction_timeout", 504, "TEXT_EXTRACTION_TIMEOUT"),
            ("extraction_process_failed", 502, "TEXT_EXTRACTION_FAILED"),
        ]
        for code, status_code, error_code in cases:
            with self.subTest(code=code):
                self.extractor.error = QuickPdfTextError(code, "safe message")
                response = self._get(f"/v1/documents/{DOC}/text")
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json()["error_code"], error_code)
                self.assertEqual(response.json()["detail"]["reason"], code)
        self.extractor.error = None
        self.assertEqual(self._get(f"/v1/documents/{DOC}/text?cursor=abc&start_page=2").status_code, 422)
        self.assertEqual(self._get(f"/v1/documents/{DOC}/text?max_chars=40001").status_code, 422)
        self.locator.located = _located(raw_file_relpath=None, raw_file_hash=None)
        unavailable = self._get(f"/v1/documents/{DOC}/text")
        self.assertEqual((unavailable.status_code, unavailable.json()["error_code"]),
                         (409, "ORIGINAL_UNAVAILABLE"))
        self.locator.located = None
        self.assertEqual(self._get(f"/v1/documents/{DOC}/text").status_code, 404)

    def test_text_capacity_is_bounded_and_logs_carry_no_query(self) -> None:
        self.slots.acquire()
        try:
            with self.assertLogs("disclosure_anchor.agent_api.access", level=logging.INFO) as logs:
                busy = self._get(f"/v1/documents/{DOC}/text?cursor=SECRETCURSORVALUE")
        finally:
            self.slots.release()
        self.assertEqual((busy.status_code, busy.json()["error_code"]), (503, "TEXT_EXTRACTION_BUSY"))
        joined = "\n".join(logs.output)
        self.assertIn("route=/v1/documents/{document_id}/text", joined)
        self.assertNotIn("SECRETCURSORVALUE", joined)
        self.assertNotIn(READ, joined)



@unittest.skipIf(TestClient is None, "fastapi is not installed")
class AgentApiCliTests(unittest.TestCase):
    ROOTS = {
        name: "/nonexistent/agent-api-cli"
        for name in ("DISCLOSURE_DATA_ROOT", "DISCLOSURE_SHARED_ROOT", "DISCLOSURE_RUNTIME_ROOT",
                     "MINERU_MODEL_CACHE", "HF_HOME", "MODELSCOPE_CACHE")
    }
    TOKEN = "SYNTHETIC-READ-TOKEN-0c4d8e2a6f1b9d3c"

    def _main(self, env: dict[str, str], argv: list[str] | None = None) -> tuple[int, str]:
        from disclosure_anchor.cli import agent_api as cli

        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("uvicorn.run", side_effect=AssertionError("the server must not start")),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli.main([] if argv is None else argv)
        return code, stderr.getvalue()

    def test_invalid_settings_name_the_variable_never_its_value(self) -> None:
        missing_root = {k: v for k, v in self.ROOTS.items() if k != "DISCLOSURE_RUNTIME_ROOT"}
        cases = {
            "token pasted into the port": (
                {**self.ROOTS, "DISCLOSURE_AGENT_API_PORT": self.TOKEN},
                "DISCLOSURE_AGENT_API_PORT [int_parsing]",
            ),
            "missing root beside a token": (
                {**missing_root, "DISCLOSURE_AGENT_API_READ_TOKEN": self.TOKEN},
                "DISCLOSURE_RUNTIME_ROOT [missing]",
            ),
        }
        for name, (env, named) in cases.items():
            with self.subTest(case=name):
                code, err = self._main(env)
                # The same status the uncaught validation error exits with.
                self.assertEqual(code, 1)
                self.assertIn("invalid agent API settings (values withheld): ", err)
                self.assertIn(named, err)
                self.assertNotIn(self.TOKEN, err)

    def test_other_startup_refusals_keep_their_messages_and_status(self) -> None:
        code, err = self._main({**self.ROOTS, "DISCLOSURE_AGENT_API_HOST": "0.0.0.0"})
        self.assertEqual((code, err.strip()), (2, "DISCLOSURE_AGENT_API_HOST must be a loopback address"))
        code, err = self._main(dict(self.ROOTS), argv=["--port", "1"])
        self.assertEqual(code, 2)
        self.assertIn("takes no arguments", err)


if __name__ == "__main__":
    unittest.main()
