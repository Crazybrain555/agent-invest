from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from disclosure_anchor.api.errors import contract_version_mismatch
from disclosure_anchor.api.pagination import (
    ChangeCursor,
    decode_change_cursor,
    encode_change_cursor,
)
from disclosure_anchor.api.routers.changes import list_changes
from disclosure_anchor.main import create_app
from disclosure_anchor.settings import SENTINEL_NAME, Settings


def _change_row(seq: int) -> dict:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    return {
        "seq": seq,
        "event_id": f"event_{seq}",
        "event_kind": "document_registered",
        "document_id": "doc_1",
        "processing_run_id": None,
        "asset_id": None,
        "payload": {"seq": seq},
        "occurred_at": now,
        "change_kind": "materialized",
        "subject_kind": "document",
        "subject_ref": "doc_1",
        "source": "disclosure_anchor",
        "contract_version": "change_event.v1",
        "created_at": now,
    }


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict]:
        return self._rows


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self._engine = engine

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, statement: object, params: dict | None = None) -> _Result:
        self._engine.statements.append(str(statement))
        self._engine.params.append(params or {})
        return _Result(self._engine.rows)


class _Engine:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.params: list[dict] = []

    def connect(self) -> _Connection:
        return _Connection(self)


def _request(engine: _Engine) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(reader_db_engine=engine)))


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


class FilingApiChangesAndErrorsTests(unittest.TestCase):
    def test_change_cursor_json_shape_is_fixed(self) -> None:
        cursor = encode_change_cursor(ChangeCursor(seq=41))
        self.assertEqual(decode_change_cursor(cursor), ChangeCursor(seq=41))

    def test_changes_after_seq_and_next_cursor(self) -> None:
        engine = _Engine([_change_row(11), _change_row(12)])

        response = list_changes(_request(engine), after_seq=10, limit=1)

        self.assertEqual([item.seq for item in response.items], [11])
        self.assertEqual(decode_change_cursor(response.next_cursor), ChangeCursor(seq=11))
        self.assertIn("WHERE seq > :after_seq", engine.statements[0])
        self.assertIn("ORDER BY seq ASC", engine.statements[0])
        self.assertEqual(engine.params[0]["after_seq"], 10)
        self.assertEqual(engine.params[0]["limit_plus_one"], 2)

    def test_changes_cursor_takes_precedence_over_after_seq(self) -> None:
        engine = _Engine([_change_row(21)])

        list_changes(
            _request(engine),
            after_seq=10,
            cursor=encode_change_cursor(ChangeCursor(seq=20)),
        )

        self.assertEqual(engine.params[0]["after_seq"], 20)

    def test_changes_empty_page_has_null_next_cursor(self) -> None:
        response = list_changes(_request(_Engine([])), after_seq=99)

        self.assertEqual(response.items, [])
        self.assertIsNone(response.next_cursor)

    def test_contract_version_mismatch_uses_error_envelope(self) -> None:
        error = contract_version_mismatch("v2")

        self.assertEqual(error.status_code, 400)
        self.assertEqual(
            error.body(),
            {
                "error_code": "CONTRACT_VERSION_MISMATCH",
                "message": "unsupported contract version",
                "detail": {"requested": "v2", "supported": ["v1"]},
            },
        )

    def test_create_app_registers_contract_version_middleware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_roots(root)
            app = create_app(_settings(root), validate_runtime=False)

        self.assertTrue(app.user_middleware)


if __name__ == "__main__":
    unittest.main()
