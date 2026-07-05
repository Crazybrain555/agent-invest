import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.api.routers.health import health_payload
from disclosure_anchor.settings import SENTINEL_NAME, Settings


class _ScalarResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _Connection:
    def __init__(self, *, value: str | None = "0008_unit_builder_provenance") -> None:
        self._value = value

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self._value)


class _Engine:
    def __init__(
        self, *, fail: bool = False, value: str | None = "0008_unit_builder_provenance"
    ) -> None:
        self._fail = fail
        self._value = value

    def connect(self) -> _Connection:
        if self._fail:
            raise RuntimeError("database unavailable")
        return _Connection(value=self._value)


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


class HealthTests(unittest.TestCase):
    def test_health_payload_ok_with_migration_head_and_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            payload = health_payload(settings=_settings(root), engine=_Engine())  # type: ignore[arg-type]
        self.assertEqual(payload.status, "ok")
        self.assertEqual(payload.service, "disclosure_anchor")
        self.assertTrue(payload.version)
        self.assertEqual(payload.migration_head, "0008_unit_builder_provenance")
        self.assertTrue(payload.data_root_mounted)

    def test_health_degrades_when_database_query_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            payload = health_payload(
                settings=_settings(root),
                engine=_Engine(fail=True),  # type: ignore[arg-type]
            )
        self.assertEqual(payload.status, "degraded")
        self.assertIsNone(payload.migration_head)
        self.assertTrue(payload.data_root_mounted)

    def test_health_degrades_when_sentinel_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            (root / SENTINEL_NAME).unlink()
            payload = health_payload(settings=_settings(root), engine=_Engine())  # type: ignore[arg-type]
        self.assertEqual(payload.status, "degraded")
        self.assertIsNone(payload.migration_head)
        self.assertFalse(payload.data_root_mounted)

    def test_health_payload_degrades_without_database_state(self) -> None:
        payload = health_payload(settings=None, engine=None)
        self.assertEqual(payload.status, "degraded")
        self.assertEqual(payload.service, "disclosure_anchor")
        self.assertTrue(payload.version)
        self.assertIsNone(payload.migration_head)
        self.assertTrue(payload.data_root_mounted)


if __name__ == "__main__":
    unittest.main()
