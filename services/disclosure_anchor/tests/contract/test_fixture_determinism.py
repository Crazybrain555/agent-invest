"""Keep legacy phase00 evidence frozen without treating it as current input."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase00"


class FixtureDeterminismTests(unittest.TestCase):
    def test_missing_raw_parser_bundle_requires_reparse(self) -> None:
        import scripts.regen_phase00_fixtures as regen

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(
                regen,
                "_read_ref",
                return_value={
                    "Content list": str(
                        Path(temp_dir) / "missing_content_list.json"
                    )
                },
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "reparse_required"):
                regen._content_list_path("annual_report")

    def test_legacy_fixtures_are_explicitly_reparse_required(self) -> None:
        import scripts.regen_phase00_fixtures as regen

        sample_dirs = [
            item
            for item in sorted(FIXTURES.iterdir())
            if (item / "document_units.v1.jsonl").exists()
        ]
        self.assertTrue(sample_dirs, "no phase00 fixtures found")
        for sample_dir in sample_dirs:
            with self.subTest(sample=sample_dir.name):
                committed = (sample_dir / "document_units.v1.jsonl").read_text(
                    encoding="utf-8"
                )
                ir = json.loads(
                    (sample_dir / "normalized_ir.v2.json").read_text(encoding="utf-8")
                )
                self.assertTrue(committed.strip())
                for line in committed.splitlines():
                    self.assertIsInstance(json.loads(line), dict)
                with self.assertRaisesRegex(
                    NormalizedIRVersionError,
                    "normalized_ir.v4",
                ):
                    regen.render_document_units_jsonl(
                        normalized_ir=ir,
                        sample_key=sample_dir.name,
                    )


if __name__ == "__main__":
    unittest.main()
