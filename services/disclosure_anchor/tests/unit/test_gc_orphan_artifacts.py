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
    _snapshot_expected_owners,
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
            "provider_documents": set(),
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
        owned_provider = self._file(
            "derived/provider_documents/cninfo/000001/pid/run/"
            "provider_document.v1.json"
        )
        sibling_provider = self._file(
            "derived/provider_documents/cninfo/000001/pid/run/unowned.json"
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
        expected["provider_documents"].add(
            "derived/provider_documents/cninfo/000001/pid/run/"
            "provider_document.v1.json"
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
            {orphan_parser, sibling_ir, sibling_provider, orphan_units},
        )
        self.assertNotIn(owned_parser, {orphan.path for orphan in orphans})
        self.assertNotIn(owned_ir, {orphan.path for orphan in orphans})
        self.assertNotIn(owned_provider, {orphan.path for orphan in orphans})
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

    def test_owner_snapshot_keeps_active_and_inactive_history(self) -> None:
        active = (
            "parser_artifacts/cninfo/000001/doc/active",
            "derived/normalized_ir/doc/active/normalized_ir.v4.json",
            "derived/provider_documents/cninfo/000001/pid/active/"
            "provider_document.v1.json",
            "derived/document_unit_snapshots/doc/active/document_units.v1.jsonl",
            "sha256:" + "a" * 64,
            "derived/document_unit_snapshots/doc/active/"
            "semantic_route_receipts.v2.jsonl",
            "semantic_route_receipt.v2",
        )
        inactive = (
            "parser_artifacts/cninfo/000001/doc/inactive",
            "derived/normalized_ir/doc/inactive/normalized_ir.v4.json",
            "derived/provider_documents/cninfo/000001/pid/inactive/"
            "provider_document.v1.json",
            "derived/document_unit_snapshots/doc/inactive/document_units.v1.jsonl",
            "sha256:" + "b" * 64,
            None,
            None,
        )
        conn = MagicMock()
        conn.execute.return_value = [active, inactive]

        expected = _snapshot_expected_owners(conn)

        statement = " ".join(
            str(conn.execute.call_args.args[0]).upper().split()
        )
        self.assertNotIn("WHERE", statement)
        self.assertNotIn("IS_ACTIVE", statement)
        self.assertEqual(expected["parser_artifacts"], {active[0], inactive[0]})
        self.assertEqual(expected["normalized_ir"], {active[1], inactive[1]})
        self.assertEqual(
            expected["provider_documents"],
            {active[2], inactive[2]},
        )
        self.assertEqual(
            expected["document_unit_snapshots"],
            {
                active[3],
                inactive[3],
                "derived/document_unit_snapshots/doc/active/"
                "semantic_route_receipts.v2.jsonl",
                "derived/document_unit_snapshots/doc/inactive/"
                "semantic_route_receipts.v1.jsonl",
            },
        )
        for relpath in (*active[:4], *inactive[:4]):
            self._file(relpath)
        self._file(
            "derived/document_unit_snapshots/doc/active/"
            "semantic_route_receipts.v2.jsonl"
        )
        self._file(
            "derived/document_unit_snapshots/doc/inactive/"
            "semantic_route_receipts.v1.jsonl"
        )
        candidates, _ = _scan_old_candidates(
            self.data_root,
            now_ts=self.now_ts,
        )
        orphans, _ = _collect_orphans(
            candidates,
            data_root=self.data_root,
            expected=expected,
            now_ts=self.now_ts,
        )
        self.assertEqual(orphans, [])

    def test_owner_snapshot_does_not_protect_unbound_receipt_sidecar(self) -> None:
        units_relpath = (
            "derived/document_unit_snapshots/doc/run/document_units.v1.jsonl"
        )
        conn = MagicMock()
        conn.execute.return_value = [(None, None, None, units_relpath, None, None, None)]
        receipt = self._file(
            "derived/document_unit_snapshots/doc/run/"
            "semantic_route_receipts.v1.jsonl"
        )

        expected = _snapshot_expected_owners(conn)
        candidates, _ = _scan_old_candidates(self.data_root, now_ts=self.now_ts)
        orphans, _ = _collect_orphans(
            candidates,
            data_root=self.data_root,
            expected=expected,
            now_ts=self.now_ts,
        )

        self.assertEqual([orphan.path for orphan in orphans], [receipt])

    def test_daily_job_is_orphan_only(self) -> None:
        service_root = Path(__file__).resolve().parents[2]
        script = (service_root / "scripts" / "gc_daily.sh").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("retire_derived_generation", script)
        self.assertNotIn("--auto", script)
        self.assertIn("gc_orphan_artifacts.py --apply", script)
        self.assertFalse(
            (service_root / "scripts" / "retire_derived_generation.py").exists()
        )
        makefile = (service_root / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn("retire-derived:", makefile)

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
        self.assertEqual(stored["manifest_schema"], "orphan-derived-artifacts.v3")
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
