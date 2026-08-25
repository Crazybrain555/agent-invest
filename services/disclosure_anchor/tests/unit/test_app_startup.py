import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from disclosure_anchor.domain.errors import ConfigurationError, MissingDependencyError
from disclosure_anchor.settings import SENTINEL_NAME, Settings
from tests.unit._env import without_db_env


def _settings(
    root: Path,
    *,
    database_url: str | None = None,
    reader_database_url: str | None = None,
) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        database_url=database_url,
        disclosure_reader_database_url=reader_database_url,
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

    def test_create_app_fails_closed_when_reader_url_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.main.create_db_engine"
        ) as create_db_engine:
            root = Path(tmp)
            _create_roots(root)
            app_engine = MagicMock()
            create_db_engine.return_value = app_engine

            with self.assertRaisesRegex(
                ConfigurationError,
                "DISCLOSURE_READER_DATABASE_URL",
            ):
                self._create_app_or_skip(
                    _settings(root, database_url="postgresql+psycopg://app/db"),
                    validate_runtime=False,
                )

        create_db_engine.assert_called_once_with("postgresql+psycopg://app/db")
        app_engine.dispose.assert_called_once_with()

    def test_create_app_uses_distinct_reader_engine_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.main.create_db_engine"
        ) as create_db_engine:
            root = Path(tmp)
            _create_roots(root)
            app_engine = object()
            reader_engine = object()
            create_db_engine.side_effect = [app_engine, reader_engine]

            app = self._create_app_or_skip(
                _settings(
                    root,
                    database_url="postgresql+psycopg://app/db",
                    reader_database_url="postgresql+psycopg://reader/db",
                ),
                validate_runtime=False,
            )

        self.assertEqual(create_db_engine.call_count, 2)
        self.assertIs(app.state.app_db_engine, app_engine)
        self.assertIs(app.state.reader_db_engine, reader_engine)
        self.assertIs(app.state.db_engine, app_engine)

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
