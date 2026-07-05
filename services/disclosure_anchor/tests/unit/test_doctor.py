import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.doctor import (
    _check_unit_snapshot_aggregate,
    run_doctor,
    run_startup_preflight,
)
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
    def test_passes_with_sentinel_writable_roots_and_external_caches(self) -> None:
        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_doctor(_settings(root))
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

    def test_startup_preflight_warns_when_reader_url_falls_back_to_app_url(self) -> None:
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
        warnings = {
            result.name: result.message
            for result in report.results
            if result.status == "WARN"
        }
        self.assertIn("DISCLOSURE_READER_DATABASE_URL", warnings)
        self.assertIn("DATABASE_URL fallback", warnings["DISCLOSURE_READER_DATABASE_URL"])

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


if __name__ == "__main__":
    unittest.main()
