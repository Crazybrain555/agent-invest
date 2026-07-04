import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.adapters.runtime.doctor import run_doctor, run_startup_preflight
from disclosure_anchor.settings import SENTINEL_NAME, Settings


def _settings(root: Path, *, bad_cache: bool = False) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    cache_root = root / "internal_cache" if bad_cache else shared_root / "model_cache"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
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
        with tempfile.TemporaryDirectory() as tmp:
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            report = run_startup_preflight(_settings(root))
            self.assertFalse(report.ok)
            failed = {result.name for result in report.results if not result.ok}
            self.assertIn("DATABASE_URL", failed)

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


if __name__ == "__main__":
    unittest.main()
