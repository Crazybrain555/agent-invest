import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.domain.errors import ConfigurationError, MissingDependencyError
from disclosure_anchor.settings import SENTINEL_NAME, Settings
from tests.unit._env import without_db_env


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


def _create_roots(root: Path) -> None:
    (root / "services" / "disclosure_anchor" / "runtime").mkdir(parents=True)
    (root / "shared" / "model_cache").mkdir(parents=True)
    (root / SENTINEL_NAME).write_text("agent-system\n", encoding="utf-8")


class AppStartupTests(unittest.TestCase):
    def _create_app_or_skip(self, settings: Settings, *, validate_runtime: bool = True):
        try:
            from disclosure_anchor.main import create_app
        except MissingDependencyError as exc:
            self.skipTest(str(exc))
        return create_app(settings, validate_runtime=validate_runtime)

    def test_create_app_can_skip_runtime_validation_for_unit_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            app = self._create_app_or_skip(_settings(root), validate_runtime=False)
            self.assertEqual(app.title, "disclosure_anchor")

    def test_create_app_fails_closed_without_database_url(self) -> None:
        with without_db_env(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            with self.assertRaises(ConfigurationError) as caught:
                self._create_app_or_skip(_settings(root))
            self.assertIn("DATABASE_URL", str(caught.exception))

    def test_create_app_fails_closed_without_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            (root / SENTINEL_NAME).unlink()
            with self.assertRaises(ConfigurationError):
                self._create_app_or_skip(_settings(root))


if __name__ == "__main__":
    unittest.main()
