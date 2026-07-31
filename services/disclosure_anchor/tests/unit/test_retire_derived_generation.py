from __future__ import annotations

from contextlib import nullcontext
import unittest
from unittest.mock import MagicMock

from scripts.retire_derived_generation import _apply_metadata


class RetireDerivedGenerationTests(unittest.TestCase):
    def test_apply_rechecks_manifest_cutoff_before_deleting_metadata(self) -> None:
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value = nullcontext(conn)
        retirable_count = MagicMock()
        retirable_count.scalar_one.return_value = 0
        present_count = MagicMock()
        present_count.scalar_one.return_value = 1
        conn.execute.side_effect = [retirable_count, present_count]
        manifest = {
            "before": "2026-07-01T00:00:00+00:00",
            "runs": [{"processing_run_id": "run_too_new"}],
        }

        with self.assertRaisesRegex(SystemExit, "retirement guards drifted"):
            _apply_metadata(engine, manifest)

        statement, parameters = conn.execute.call_args_list[0].args
        self.assertIn("pr.created_at < :before", str(statement))
        self.assertIn(
            "dependent.artifact_owner_processing_run_id",
            str(statement),
        )
        self.assertEqual(parameters["before"], manifest["before"])
        self.assertEqual(conn.execute.call_count, 2)

    def test_apply_rejects_manifest_without_cutoff_before_opening_transaction(
        self,
    ) -> None:
        engine = MagicMock()

        with self.assertRaisesRegex(SystemExit, "no valid before cutoff"):
            _apply_metadata(
                engine,
                {"runs": [{"processing_run_id": "run_1"}]},
            )

        engine.begin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
