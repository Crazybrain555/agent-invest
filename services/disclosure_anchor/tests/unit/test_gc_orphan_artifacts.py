from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from scripts.gc_orphan_artifacts import (
    _Candidate,
    _build_manifest,
    _collect_orphans,
    _scan_old_candidates,
    _write_manifest_before_delete,
    main,
)


class OrphanDerivedArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.data_root = Path(self._tempdir.name) / "data"
        self.now_ts = time.time()

    def _file(self, relpath: str, *, age_seconds: int = 25 * 3600) -> Path:
        path = self.data_root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relpath, encoding="utf-8")
        old = self.now_ts - age_seconds
        os.utime(path, (old, old))
        return path

    @staticmethod
    def _expected() -> dict[str, set[str]]:
        return {
            "parser_artifacts": set(),
            "normalized_ir": set(),
            "document_unit_snapshots": set(),
        }

    def test_family_ownership_uses_prefix_only_for_parser_artifacts(self) -> None:
        owned_parser = self._file(
            "parser_artifacts/cninfo/000001/doc/run/child.json"
        )
        orphan_parser = self._file(
            "parser_artifacts/cninfo/000001/doc/retired/child.json"
        )
        owned_ir = self._file(
            "derived/normalized_ir/doc/run/normalized_ir.v3.json"
        )
        sibling_ir = self._file(
            "derived/normalized_ir/doc/run/unowned-sibling.json"
        )
        owned_units = self._file(
            "derived/document_unit_snapshots/doc/run/document_units.v1.jsonl"
        )
        orphan_units = self._file(
            "derived/document_unit_snapshots/doc/retired/"
            "document_units.v1.jsonl"
        )
        expected = self._expected()
        expected["parser_artifacts"].add(
            "parser_artifacts/cninfo/000001/doc/run"
        )
        expected["normalized_ir"].add(
            "derived/normalized_ir/doc/run/normalized_ir.v3.json"
        )
        expected["document_unit_snapshots"].add(
            "derived/document_unit_snapshots/doc/run/"
            "document_units.v1.jsonl"
        )

        candidates, skipped = _scan_old_candidates(
            self.data_root,
            now_ts=self.now_ts,
        )
        orphans, recheck_skipped = _collect_orphans(
            candidates,
            data_root=self.data_root,
            expected=expected,
            now_ts=self.now_ts,
        )

        self.assertEqual(sum(skipped.values()), 0)
        self.assertEqual(sum(recheck_skipped.values()), 0)
        self.assertEqual(
            {orphan.path for orphan in orphans},
            {orphan_parser, sibling_ir, orphan_units},
        )
        self.assertNotIn(owned_parser, {orphan.path for orphan in orphans})
        self.assertNotIn(owned_ir, {orphan.path for orphan in orphans})
        self.assertNotIn(owned_units, {orphan.path for orphan in orphans})

    def test_age_is_rechecked_after_the_ownership_snapshot(self) -> None:
        path = self._file(
            "derived/normalized_ir/doc/run/normalized_ir.v3.json"
        )
        candidates, initially_skipped = _scan_old_candidates(
            self.data_root,
            now_ts=self.now_ts,
        )
        os.utime(path, (self.now_ts, self.now_ts))

        orphans, recheck_skipped = _collect_orphans(
            candidates,
            data_root=self.data_root,
            expected=self._expected(),
            now_ts=self.now_ts,
        )

        self.assertEqual(initially_skipped["normalized_ir"], 0)
        self.assertEqual(orphans, [])
        self.assertEqual(recheck_skipped["normalized_ir"], 1)

    def test_manifest_records_family_and_file_identity_before_deletion(
        self,
    ) -> None:
        path = self._file(
            "derived/document_unit_snapshots/doc/run/"
            "document_units.v1.jsonl"
        )
        candidates = [
            _Candidate(family="document_unit_snapshots", path=path)
        ]
        orphans, _ = _collect_orphans(
            candidates,
            data_root=self.data_root,
            expected=self._expected(),
            now_ts=self.now_ts,
        )
        manifest = _build_manifest(
            orphans,
            planned_at="2026-07-27T00:00:00Z",
        )
        audit_path = Path(self._tempdir.name) / "audit" / "manifest.json"
        audit_path.parent.mkdir(parents=True)

        _write_manifest_before_delete(audit_path, manifest)

        stored = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["manifest_schema"], "orphan-derived-artifacts.v2")
        self.assertEqual(stored["families"]["document_unit_snapshots"]["files"], 1)
        self.assertEqual(
            stored["files"][0]["relpath"],
            "derived/document_unit_snapshots/doc/run/"
            "document_units.v1.jsonl",
        )
        with self.assertRaises(FileExistsError):
            _write_manifest_before_delete(audit_path, manifest)

    def test_apply_holds_exclusive_gate_from_owner_snapshot_through_unlink(
        self,
    ) -> None:
        orphan = self._file(
            "derived/normalized_ir/doc/run/normalized_ir.v3.json"
        )
        settings = MagicMock()
        settings.disclosure_data_root = Path(self._tempdir.name)
        settings.database_url.get_secret_value.return_value = "unused"
        engine = MagicMock()
        engine.connect.return_value = nullcontext(object())
        gate_state = {"held": False}

        @contextmanager
        def mutation_gate(_engine: object) -> Iterator[None]:
            gate_state["held"] = True
            try:
                yield
                self.assertFalse(orphan.exists())
            finally:
                gate_state["held"] = False

        def snapshot(_conn: object) -> dict[str, set[str]]:
            self.assertTrue(gate_state["held"])
            return self._expected()

        with (
            patch(
                "scripts.gc_orphan_artifacts.load_settings",
                return_value=settings,
            ),
            patch(
                "scripts.gc_orphan_artifacts.create_db_engine",
                return_value=engine,
            ),
            patch(
                "scripts.gc_orphan_artifacts.exclusive_corpus_mutation",
                side_effect=mutation_gate,
            ),
            patch(
                "scripts.gc_orphan_artifacts._snapshot_expected_owners",
                side_effect=snapshot,
            ),
            patch.object(sys, "argv", ["gc_orphan_artifacts.py", "--apply"]),
        ):
            self.assertEqual(main(), 0)

        self.assertFalse(gate_state["held"])
        self.assertFalse(orphan.exists())
        manifests = list(
            (Path(self._tempdir.name) / "audit" / "gc").glob("*.json")
        )
        self.assertEqual(len(manifests), 1)
        engine.dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
