"""The retired destructive fresh-start planner must stay fail closed."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import unittest

from scripts.plan_preproduction_fresh_start import main


class PreproductionFreshStartPlanTests(unittest.TestCase):
    def test_command_is_a_fail_closed_tombstone(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = main()

        self.assertEqual(result, 2)
        message = stderr.getvalue()
        self.assertIn("[STOP]", message)
        self.assertIn("preserve source_access", message)
        self.assertIn("published processing runs/Units", message)


if __name__ == "__main__":
    unittest.main()
