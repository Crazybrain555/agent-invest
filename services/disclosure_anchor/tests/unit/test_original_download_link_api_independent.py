"""Independent private-route tests; no database, provider or original file IO."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError

from disclosure_anchor.api.agent_auth import AgentApiAccess
from disclosure_anchor.api.agent_errors import install_agent_error_handlers
from disclosure_anchor.api.errors import install_error_handlers
from disclosure_anchor.api.routers.original_download_link import router
from disclosure_anchor.application.ports.original_download_link import OriginalDownloadLinkRecord
from disclosure_anchor.application.use_cases.read_original_download_link import ReadOriginalDownloadLink
from disclosure_anchor.main import create_app
from disclosure_anchor.settings import Settings


class OriginalDownloadLinkApiIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = "doc_00000000000000000000000001"
        self.path = "/v1/documents/" + self.document + "/download-link"
        self.record = OriginalDownloadLinkRecord(
            self.document, "cninfo", "synthetic", "sha256:" + "a" * 64,
            "sa_synthetic", "https://static.cninfo.com.cn/finalpage/synthetic.pdf",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.reader = mock.Mock()
        self.reader.read.return_value = self.record
        self.headers = {"Authorization": "Bearer " + "r" * 32}

    def _client(self, *, raise_server_exceptions=True):
        app = FastAPI()
        install_error_handlers(app)
        install_agent_error_handlers(app)
        app.state.agent_api_access = AgentApiAccess(b"r" * 32, b"w" * 32, frozenset({"testserver"}))
        app.state.original_download_link = ReadOriginalDownloadLink(self.reader)
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    def test_read_scope_host_origin_and_no_query_are_enforced_before_reader(self) -> None:
        with self._client() as client:
            for headers, expected in (
                ({}, 401), ({"Authorization": "Bearer " + "w" * 32}, 403),
                ({**self.headers, "Host": "external.invalid"}, 403),
                ({**self.headers, "Origin": "https://external.invalid"}, 403),
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(client.get(self.path, headers=headers).status_code, expected)
            self.assertEqual(client.get(self.path + "?url=file:///private.pdf", headers=self.headers).status_code, 422)
            self.assertEqual(client.post(self.path, headers=self.headers).status_code, 405)
            self.reader.read.assert_not_called()
            response = client.get(self.path, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["url"], self.record.recorded_url)
        self.assertFalse(response.json()["link_live_checked"])
        self.reader.read.assert_called_once_with(self.document)

    def test_missing_document_no_link_and_no_archive_hash_remain_distinct(self) -> None:
        with self._client() as client:
            self.reader.read.return_value = None
            response = client.get(self.path, headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error_code"], "NOT_FOUND")
            self.reader.read.return_value = replace(self.record, recorded_url=None, raw_file_hash=None)
            response = client.get(self.path, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["unavailable_reason"], "no_matching_recorded_url")
            self.assertFalse(response.json()["hash_verification_required"])
            self.assertIsNone(response.json()["url"])
            self.reader.read.reset_mock()
            response = client.get("/v1/documents/bad/download-link", headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.reader.read.assert_not_called()

    def test_sql_failure_family_is_unavailable_not_missing_link_or_private_detail(self) -> None:
        for error_type in (OperationalError, ProgrammingError):
            with self.subTest(error_type=error_type.__name__):
                self.reader.read.side_effect = error_type("SELECT private_table", {}, RuntimeError("private-secret"))
                with self._client(raise_server_exceptions=False) as client:
                    response = client.get(self.path, headers=self.headers)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error_code"], "SERVICE_UNAVAILABLE")
                self.assertNotIn("private-secret", response.text)
                self.assertNotIn("private_table", response.text)

    def test_public_filing_application_does_not_mount_private_download_link(self) -> None:
        # Explicit null DB settings block inherited shell configuration; no
        # runtime preflight, database connection or source fetch can occur.
        settings = Settings(_env_file=None, database_url=None,
                            disclosure_reader_database_url=None,
                            disclosure_admin_database_url=None,
                            disclosure_migration_database_url=None,
                            disclosure_data_root=Path("/tmp/link-offline/services/disclosure_anchor"),
                            disclosure_shared_root=Path("/tmp/link-offline/shared"),
                            disclosure_runtime_root=Path("/tmp/link-offline/services/disclosure_anchor/runtime"),
                            mineru_model_cache=Path("/tmp/link-offline/shared/models"),
                            hf_home=Path("/tmp/link-offline/shared/hf"),
                            modelscope_cache=Path("/tmp/link-offline/shared/modelscope"))
        with mock.patch("disclosure_anchor.main.create_db_engine", side_effect=AssertionError("unexpected database")):
            app = create_app(settings, validate_runtime=False)
        with TestClient(app) as client:
            response = client.get(self.path, headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("/v1/documents/{document_id}/download-link", app.openapi()["paths"])
        self.reader.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
