"""Tests for doctor report rendering."""

import unittest

from disclosure_anchor.adapters.runtime.doctor import CheckResult, render_report


class RenderReportTests(unittest.TestCase):
    def test_renders_one_line_per_result(self) -> None:
        results = (
            CheckResult(name="agent_system_root", status="PASS", message="/x"),
            CheckResult(name="outbox seq", status="WARN", message="gap detected"),
            CheckResult(name="mount sentinel", status="FAIL", message="missing: /x/SENT"),
        )
        rendered = render_report(results)
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "[PASS] agent_system_root: /x")
        self.assertEqual(lines[1], "[WARN] outbox seq: gap detected")
        self.assertEqual(lines[2], "[FAIL] mount sentinel: missing: /x/SENT")

    def test_check_result_ok_property(self) -> None:
        self.assertTrue(CheckResult(name="n", status="PASS", message="m").ok)
        self.assertTrue(CheckResult(name="n", status="WARN", message="m").ok)
        self.assertFalse(CheckResult(name="n", status="FAIL", message="m").ok)


if __name__ == "__main__":
    unittest.main()
