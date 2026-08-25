import hashlib
import json
import http.client
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.db.postgres.connection import (
    RuntimeDatabaseIdentity,
    require_runtime_app_connection,
    require_runtime_reader_connection,
)
from disclosure_anchor.adapters.runtime.doctor import (
    CheckResult,
    _check_unit_snapshot_aggregate,
    _invalid_process_class_overrides,
    _semantic_receipt_check,
    mineru_remote_inference_check,
    inventory_orphan_files,
    running_run_liveness_checks,
    run_doctor,
    run_startup_preflight,
)
from disclosure_anchor.domain.errors import ConfigurationError
from disclosure_anchor.domain.services.unit_hashing import content_hash_aggregate
from disclosure_anchor.settings import SENTINEL_NAME, Settings
from tests.unit._env import without_db_env


def _settings(
    root: Path,
    *,
    bad_cache: bool = False,
    database_url: str | None = None,
    reader_database_url: str | None = None,
) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    cache_root = root / "internal_cache" if bad_cache else shared_root / "model_cache"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        database_url=database_url,
        disclosure_reader_database_url=reader_database_url,
        mineru_model_cache=cache_root / "mineru",
        hf_home=cache_root / "huggingface",
        modelscope_cache=cache_root / "modelscope",
    )


def _create_roots(root: Path) -> None:
    (root / "services" / "disclosure_anchor" / "runtime").mkdir(parents=True)
    (root / "shared" / "model_cache").mkdir(parents=True)
    (root / SENTINEL_NAME).write_text("agent-system\n", encoding="utf-8")


class DoctorTests(unittest.TestCase):
    def test_doctor_verifies_hash_bound_historical_semantic_receipt(self) -> None:
        payload = {
            "asset_id": "asset_1",
            "order_index": 1,
            "semantic_route": {
                "adjudication": None,
                "candidate_keys": [],
                "contract_version": "semantic_route_receipt.v2",
                "decision_source": "fallback",
                "evidence": [],
                "input_hash": "sha256:" + "1" * 64,
                "router_version": "semantic_router.v98",
                "selected_keys": [],
                "taxonomy_version": "semantic-taxonomy-2026-08-r62",
            },
        }
        raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            relpath = Path("derived/test/historical-semantic-receipt.v2.jsonl")
            path = root / "services" / "disclosure_anchor" / "data" / relpath
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)

            result = _semantic_receipt_check(
                settings=_settings(root),
                object_id="run_historical",
                relpath=str(relpath),
                contract_version="semantic_route_receipt.v2",
                expected_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
                semantic_status="not_required",
                expected_degraded_count=0,
                summary={"status": "not_required"},
            )

        self.assertEqual(result.status, "PASS")
        self.assertIn("verified", result.message)

    def test_runtime_identity_requires_direct_non_superuser_app_login(self) -> None:
        cases = (
            (
                {
                    "database_name": "other_db",
                    "session_role": "disclosure_app",
                    "current_role": "disclosure_app",
                    "session_superuser": False,
                    "current_superuser": False,
                },
                "database must be invest_engine",
            ),
            (
                {
                    "database_name": "invest_engine",
                    "session_role": "postgres",
                    "current_role": "disclosure_app",
                    "session_superuser": True,
                    "current_superuser": False,
                },
                "session_user/current_user must both be disclosure_app",
            ),
            (
                {
                    "database_name": "invest_engine",
                    "session_role": "disclosure_app",
                    "current_role": "disclosure_app",
                    "session_superuser": True,
                    "current_superuser": False,
                },
                "must not be superuser",
            ),
        )
        for row, message in cases:
            with self.subTest(row=row):
                connection = MagicMock()
                connection.execute.return_value.mappings.return_value.one.return_value = row
                with self.assertRaisesRegex(ConfigurationError, message):
                    require_runtime_app_connection(connection)

    def test_runtime_identity_accepts_only_exact_app_identity(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.one.return_value = {
            "database_name": "invest_engine",
            "session_role": "disclosure_app",
            "current_role": "disclosure_app",
            "session_superuser": False,
            "current_superuser": False,
        }

        identity = require_runtime_app_connection(connection)

        self.assertEqual(identity.database_name, "invest_engine")
        query = str(connection.execute.call_args.args[0])
        self.assertIn("current_database()", query)
        self.assertIn("session_user", query)
        self.assertIn("current_user", query)
        self.assertIn("pg_roles", query)

    def test_runtime_reader_identity_requires_direct_non_superuser_reader_login(
        self,
    ) -> None:
        cases = (
            (
                {
                    "database_name": "other_db",
                    "session_role": "disclosure_reader",
                    "current_role": "disclosure_reader",
                    "session_superuser": False,
                    "current_superuser": False,
                },
                "database must be invest_engine",
            ),
            (
                {
                    "database_name": "invest_engine",
                    "session_role": "disclosure_app",
                    "current_role": "disclosure_reader",
                    "session_superuser": False,
                    "current_superuser": False,
                },
                "session_user/current_user must both be disclosure_reader",
            ),
            (
                {
                    "database_name": "invest_engine",
                    "session_role": "disclosure_reader",
                    "current_role": "disclosure_reader",
                    "session_superuser": True,
                    "current_superuser": False,
                },
                "must not be superuser",
            ),
        )
        for row, message in cases:
            with self.subTest(row=row):
                connection = MagicMock()
                connection.execute.return_value.mappings.return_value.one.return_value = row
                with self.assertRaisesRegex(ConfigurationError, message):
                    require_runtime_reader_connection(connection)

    def test_runtime_reader_identity_accepts_only_exact_reader_identity(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.one.return_value = {
            "database_name": "invest_engine",
            "session_role": "disclosure_reader",
            "current_role": "disclosure_reader",
            "session_superuser": False,
            "current_superuser": False,
        }

        identity = require_runtime_reader_connection(connection)

        self.assertEqual(identity.session_role, "disclosure_reader")

    def test_doctor_reports_runtime_role_violation(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.scalar_one_or_none.return_value = "0050"
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        identity = RuntimeDatabaseIdentity(
            database_name="invest_engine",
            session_role="postgres",
            current_role="disclosure_app",
            session_superuser=True,
            current_superuser=False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            with (
                patch(
                    "disclosure_anchor.adapters.runtime.doctor."
                    "inspect_runtime_database_identity",
                    return_value=identity,
                ),
                patch(
                    "disclosure_anchor.adapters.runtime.doctor."
                    "single_migration_head",
                    return_value="0050",
                ),
                patch(
                    "disclosure_anchor.adapters.runtime.doctor."
                    "_classification_rules_check",
                    return_value=CheckResult("rules", "PASS", "ok"),
                ),
            ):
                report = run_startup_preflight(
                    _settings(
                        root,
                        database_url="postgresql+psycopg://app/db",
                    ),
                    engine=engine,
                )

        role_check = next(
            item for item in report.results if item.name == "runtime DB role"
        )
        self.assertEqual(role_check.status, "FAIL")
        self.assertIn("postgres/disclosure_app", role_check.message)

    def test_mineru_remote_inference_check_exercises_image_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp)).model_copy(
                update={"disclosure_mineru_observability_url": "http://gpu:30000"}
            )
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "mineru-model"}]}
        ).encode()
        completion = MagicMock()
        completion.__enter__.return_value.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "M7"},
                    }
                ]
            }
        ).encode()
        opener = MagicMock()
        opener.open.side_effect = [models, completion]

        with patch(
            "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
            return_value=opener,
        ):
            result = mineru_remote_inference_check(settings)

        self.assertEqual(result.status, "PASS")
        request = opener.open.call_args_list[1].args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "mineru-model")
        self.assertEqual(
            payload["messages"][0]["content"][1]["type"],
            "image_url",
        )

    def test_mineru_remote_inference_check_fails_on_remote_5xx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp)).model_copy(
                update={"disclosure_mineru_observability_url": "http://gpu:30000"}
            )
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "http://gpu:30000/v1/models",
            500,
            "internal error",
            hdrs=None,
            fp=None,
        )

        with patch(
            "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
            return_value=opener,
        ):
            result = mineru_remote_inference_check(settings)

        self.assertEqual(result.status, "FAIL")

    def test_mineru_remote_inference_check_fails_closed_on_bad_http_status_line(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp)).model_copy(
                update={"disclosure_mineru_observability_url": "http://gpu:30000"}
            )
        opener = MagicMock()
        opener.open.side_effect = http.client.BadStatusLine("partial response")

        with patch(
            "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
            return_value=opener,
        ):
            result = mineru_remote_inference_check(settings)

        self.assertEqual(result.status, "FAIL")

    def test_mineru_remote_inference_check_accepts_v1_api_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp)).model_copy(
                update={"disclosure_mineru_observability_url": "http://gpu:30000/v1"}
            )
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "mineru-model"}]}
        ).encode()
        completion = MagicMock()
        completion.__enter__.return_value.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "M7"},
                    }
                ]
            }
        ).encode()
        opener = MagicMock()
        opener.open.side_effect = [models, completion]

        with patch(
            "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
            return_value=opener,
        ):
            result = mineru_remote_inference_check(settings)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            opener.open.call_args_list[0].args[0],
            "http://gpu:30000/v1/models",
        )
        self.assertEqual(
            opener.open.call_args_list[1].args[0].full_url,
            "http://gpu:30000/v1/chat/completions",
        )

    def test_running_run_liveness_separates_parse_runaway_from_stale_work(
        self,
    ) -> None:
        conn = MagicMock()
        parse_result = MagicMock()
        parse_result.all.return_value = [("run_parse",)]
        other_result = MagicMock()
        other_result.all.return_value = []
        conn.execute.side_effect = [parse_result, other_result]
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))

        results = running_run_liveness_checks(settings, conn)

        self.assertEqual(
            [(item.name, item.status) for item in results],
            [("runaway parse runs", "WARN"), ("stale runs", "PASS")],
        )
        parse_sql = str(conn.execute.call_args_list[0].args[0])
        other_sql = str(conn.execute.call_args_list[1].args[0])
        self.assertIn("run_kind = 'parse'", parse_sql)
        self.assertIn("run_kind <> 'parse'", other_sql)
        self.assertEqual(
            conn.execute.call_args_list[0].args[1]["seconds"],
            settings.disclosure_parse_runaway_timeout_seconds,
        )
        self.assertEqual(
            conn.execute.call_args_list[1].args[1]["seconds"],
            settings.disclosure_stale_run_threshold_seconds,
        )

    @patch(
        "disclosure_anchor.adapters.runtime.doctor._ops_launchd_check",
        return_value=CheckResult("postgres autostart", "PASS", "test"),
    )
    @patch("disclosure_anchor.adapters.runtime.doctor.subprocess.run")
    def test_active_worker_mineru_descendants_are_not_orphans(
        self, run: MagicMock, _ops_check: MagicMock
    ) -> None:
        del _ops_check
        run.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=(
                "100 1 /bin/zsh scripts/run_worker_once.sh loop\n"
                "101 100 .venv/bin/python -m disclosure_anchor.cli.worker loop\n"
                "102 1 .venv/bin/python -m disclosure_anchor.cli.pipeline run\n"
                "103 1 .venv/bin/python -m uvicorn disclosure_anchor.main:app\n"
                "200 101 /runtime/bin/mineru -p report.pdf\n"
                "201 200 /runtime/bin/python -m mineru.cli.fast_api\n"
                "202 101 mineru -p bare-path.pdf\n"
                "203 102 /runtime/bin/mineru -p pipeline.pdf\n"
                "204 103 /runtime/bin/mineru -p admin-api.pdf\n"
                "300 1 python -c from doctor import _mineru_orphan_check\n"
            )
        )

        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_doctor(
                _settings(
                    root,
                    reader_database_url="postgresql+psycopg://reader/db",
                )
            )
        result = next(item for item in report.results if item.name == "mineru orphans")

        self.assertEqual(result.status, "PASS")
        self.assertIn("5 active process(es)", result.message)

    @patch(
        "disclosure_anchor.adapters.runtime.doctor._ops_launchd_check",
        return_value=CheckResult("postgres autostart", "PASS", "test"),
    )
    @patch("disclosure_anchor.adapters.runtime.doctor.subprocess.run")
    def test_reparented_mineru_process_is_reported_as_orphan(
        self, run: MagicMock, _ops_check: MagicMock
    ) -> None:
        del _ops_check
        run.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=(
                "1 0 /sbin/launchd\n"
                "200 1 /runtime/bin/mineru -p report.pdf\n"
                "201 200 /runtime/bin/python -m mineru.cli.fast_api\n"
            )
        )

        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_doctor(_settings(root))
        result = next(item for item in report.results if item.name == "mineru orphans")

        self.assertEqual(result.status, "WARN")
        self.assertIn("count=2", result.message)
        self.assertIn("sample_pids=200,201", result.message)

    @patch(
        "disclosure_anchor.adapters.runtime.doctor._ops_launchd_check",
        return_value=CheckResult("postgres autostart", "PASS", "test"),
    )
    @patch("disclosure_anchor.adapters.runtime.doctor.subprocess.run")
    def test_ps_failure_does_not_false_pass_orphan_check(
        self, run: MagicMock, _ops_check: MagicMock
    ) -> None:
        del _ops_check
        run.return_value = SimpleNamespace(
            returncode=1,
            stderr="process table unavailable",
            stdout="",
        )

        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_doctor(_settings(root))
        result = next(item for item in report.results if item.name == "mineru orphans")

        self.assertEqual(result.status, "WARN")
        self.assertIn("ps failed with exit 1", result.message)

    def test_override_shape_check_handles_nested_json_without_crashing(self) -> None:
        rows = [
            SimpleNamespace(
                tracked_company_id="tc_bad",
                process_classes=["annual_report", {"bad": True}, ["nested"]],
            ),
            SimpleNamespace(
                tracked_company_id="tc_mixed",
                process_classes=["annual_report", "not_a_class"],
            ),
        ]

        invalid = _invalid_process_class_overrides(
            rows, known_classes={"annual_report"}
        )

        self.assertIn("tc_bad:non-string=dict,list", invalid)
        self.assertIn("tc_mixed:unknown=not_a_class", invalid)

    @patch(
        "disclosure_anchor.adapters.runtime.doctor._ops_launchd_check",
        return_value=CheckResult("postgres autostart", "PASS", "test"),
    )
    def test_passes_with_sentinel_writable_roots_and_external_caches(
        self, _ops_check: MagicMock
    ) -> None:
        del _ops_check
        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_doctor(
                _settings(
                    root,
                    reader_database_url="postgresql+psycopg://reader/db",
                )
            )
            self.assertTrue(report.ok, report.results)
            self.assertIn(
                "raw archive filesystem",
                [result.name for result in report.results],
            )
            self.assertIn(
                "DATABASE_URL",
                [result.name for result in report.results if result.status == "WARN"],
            )

    def test_startup_preflight_fails_when_database_is_missing(self) -> None:
        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_startup_preflight(_settings(root))
            self.assertFalse(report.ok)
            failed = {result.name for result in report.results if not result.ok}
            self.assertIn("DATABASE_URL", failed)

    def test_startup_preflight_fails_when_reader_url_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.adapters.runtime.doctor._database_ping_and_migration_checks",
            return_value=[],
        ):
            root = Path(tmp)
            _create_roots(root)
            report = run_startup_preflight(
                _settings(root, database_url="postgresql+psycopg://app/db"),
                engine=object(),  # type: ignore[arg-type]
            )
        failures = {
            result.name: result.message
            for result in report.results
            if result.status == "FAIL"
        }
        self.assertIn("DISCLOSURE_READER_DATABASE_URL", failures)
        self.assertIn("exact reader role", failures["DISCLOSURE_READER_DATABASE_URL"])

    def test_startup_preflight_passes_reader_url_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.adapters.runtime.doctor._database_ping_and_migration_checks",
            return_value=[],
        ):
            root = Path(tmp)
            _create_roots(root)
            report = run_startup_preflight(
                _settings(
                    root,
                    database_url="postgresql+psycopg://app/db",
                    reader_database_url="postgresql+psycopg://reader/db",
                ),
                engine=object(),  # type: ignore[arg-type]
            )
        passes = {result.name for result in report.results if result.status == "PASS"}
        self.assertIn("DISCLOSURE_READER_DATABASE_URL", passes)

    def test_fails_closed_when_sentinel_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            (root / SENTINEL_NAME).unlink()
            report = run_doctor(_settings(root))
            self.assertFalse(report.ok)
            self.assertIn("mount sentinel", [result.name for result in report.results if not result.ok])

    def test_fails_when_model_cache_escapes_shared_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_doctor(_settings(root, bad_cache=True))
            self.assertFalse(report.ok)
            failed = {result.name for result in report.results if not result.ok}
            self.assertIn("MINERU_MODEL_CACHE", failed)
            self.assertIn("HF_HOME", failed)
            self.assertIn("MODELSCOPE_CACHE", failed)


class UnitSnapshotAggregateCheckTests(unittest.TestCase):
    RELPATH = "derived/document_unit_snapshots/x/y/z/run/document_units.v1.jsonl"

    def _write_snapshot(self, root: Path, rows: list[dict[str, object]]) -> None:
        path = (
            root / "services" / "disclosure_anchor" / "data" / Path(self.RELPATH)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def test_passes_when_recomputed_aggregate_matches(self) -> None:
        hashes = ["sha256:bbb", "sha256:aaa", "sha256:aaa"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            self._write_snapshot(root, [{"content_hash": value} for value in hashes])
            result = _check_unit_snapshot_aggregate(
                settings=_settings(root),
                object_id="run_x",
                relpath=self.RELPATH,
                expected_aggregate=content_hash_aggregate(hashes),
            )
            self.assertEqual(result.status, "PASS", result.message)

    def test_fails_on_aggregate_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            self._write_snapshot(root, [{"content_hash": "sha256:aaa"}])
            result = _check_unit_snapshot_aggregate(
                settings=_settings(root),
                object_id="run_x",
                relpath=self.RELPATH,
                expected_aggregate=content_hash_aggregate(["sha256:other"]),
            )
            self.assertFalse(result.ok)
            self.assertIn("aggregate mismatch", result.message)

    def test_fails_when_snapshot_row_lacks_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            self._write_snapshot(root, [{"asset_id": "du_x"}])
            result = _check_unit_snapshot_aggregate(
                settings=_settings(root),
                object_id="run_x",
                relpath=self.RELPATH,
                expected_aggregate="sha256:whatever",
            )
            self.assertFalse(result.ok)
            self.assertIn("missing content_hash", result.message)

    def test_fails_when_snapshot_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            result = _check_unit_snapshot_aggregate(
                settings=_settings(root),
                object_id="run_x",
                relpath=self.RELPATH,
                expected_aggregate="sha256:whatever",
            )
            self.assertFalse(result.ok)
            self.assertIn("missing file", result.message)


class OrphanFileInventoryTests(unittest.TestCase):
    def test_prefix_mode_matches_only_exact_path_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            parser_root = data_root / "parser_artifacts"
            owned_nested = parser_root / "run_1" / "nested" / "content.json"
            owned_exact = parser_root / "standalone.json"
            prefix_collision = parser_root / "run_10" / "content.json"
            sibling = parser_root / "run_2" / "content.json"
            for path in (
                owned_nested,
                owned_exact,
                prefix_collision,
                sibling,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            orphans = inventory_orphan_files(
                parser_root,
                data_root=data_root,
                expected_relpaths={
                    "parser_artifacts/run_1",
                    "parser_artifacts/standalone.json",
                },
                prefix_match=True,
            )

        self.assertEqual(
            sorted(orphans),
            [
                "parser_artifacts/run_10/content.json",
                "parser_artifacts/run_2/content.json",
            ],
        )

    def test_empty_expected_set_marks_every_file_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            parser_root = data_root / "parser_artifacts"
            path = parser_root / "run" / "content.json"
            path.parent.mkdir(parents=True)
            path.write_text("x", encoding="utf-8")

            orphans = inventory_orphan_files(
                parser_root,
                data_root=data_root,
                expected_relpaths=set(),
                prefix_match=True,
            )

        self.assertEqual(orphans, ["parser_artifacts/run/content.json"])

    def test_exact_mode_does_not_treat_directory_as_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            raw_root = data_root / "raw_documents"
            path = raw_root / "owner" / "document.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"%PDF-")

            orphans = inventory_orphan_files(
                raw_root,
                data_root=data_root,
                expected_relpaths={"raw_documents/owner"},
                prefix_match=False,
            )

        self.assertEqual(orphans, ["raw_documents/owner/document.pdf"])


if __name__ == "__main__":
    unittest.main()
