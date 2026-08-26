from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorHealth,
)
from scripts.mineru_smoke import (
    RECEIPT_SCHEMA,
    _smoke_orchestrator_evidence,
    run_cli,
)


class MinerUSmokeCliReceiptTests(unittest.TestCase):
    def test_v4_evidence_accepts_retained_gauge_cleanup_without_deltas(self) -> None:
        def health(*, completed: int, failed: int) -> MinerUOrchestratorHealth:
            return MinerUOrchestratorHealth(
                status="healthy",
                version="3.4.4",
                protocol_version=2,
                queued_tasks=0,
                processing_tasks=0,
                completed_tasks=completed,
                failed_tasks=failed,
                max_concurrent_requests=1,
                processing_window_size=16,
                task_retention_seconds=600,
                task_cleanup_interval_seconds=30,
            )

        evidence = _smoke_orchestrator_evidence(
            health(completed=2, failed=1),
            health(completed=0, failed=0),
        )

        self.assertEqual(RECEIPT_SCHEMA, "mineru_smoke_receipt.v4")
        self.assertEqual(
            evidence["task_registry_semantics"],
            "retained-terminal-gauges.v1",
        )
        self.assertNotIn("completed_delta", evidence)
        self.assertNotIn("failed_delta", evidence)

    def test_operational_abort_writes_new_redacted_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.mineru_smoke.main",
            side_effect=SystemExit(
                "[abort] remote https://user:secret@gpu.invalid failed"
            ),
        ):
            receipt = Path(tmp) / "fail.json"
            with self.assertRaises(SystemExit):
                run_cli(["--receipt-out", str(receipt)])
            payload = json.loads(receipt.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["database_access"], "none")
        self.assertIn("started_at_utc", payload)
        self.assertEqual(payload["cleanup"]["status"], "not_proved")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertIn("<redacted-url>", payload["failure"]["detail"])

    def test_argparse_help_and_error_do_not_create_failure_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            help_receipt = Path(tmp) / "help.json"
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as help_exit:
                run_cli(["--receipt-out", str(help_receipt), "--help"])
            error_receipt = Path(tmp) / "error.json"
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error_exit:
                run_cli(["--receipt-out", str(error_receipt), "--unknown"])

        self.assertEqual(help_exit.exception.code, 0)
        self.assertEqual(error_exit.exception.code, 2)
        self.assertFalse(help_receipt.exists())
        self.assertFalse(error_receipt.exists())

    def test_existing_receipt_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.mineru_smoke.main",
            side_effect=SystemExit("[abort] operational failure"),
        ):
            receipt = Path(tmp) / "existing.json"
            receipt.write_text("keep-me\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                run_cli(["--receipt-out", str(receipt)])
            content = receipt.read_text(encoding="utf-8")

        self.assertEqual(content, "keep-me\n")

    def test_unexpected_operational_exception_writes_failure_receipt(self) -> None:
        original = RuntimeError("unexpected operational failure")
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.mineru_smoke.main",
            side_effect=original,
        ):
            receipt = Path(tmp) / "unexpected.json"
            with self.assertRaises(RuntimeError) as caught:
                run_cli(["--receipt-out", str(receipt)])
            payload = json.loads(receipt.read_text(encoding="utf-8"))

        self.assertIs(caught.exception, original)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["failure"]["exception_type"], "RuntimeError")

    def test_receipt_write_failure_never_masks_original_abort(self) -> None:
        original = SystemExit("[abort] original failure")
        with patch("scripts.mineru_smoke.main", side_effect=original), patch(
            "scripts.mineru_smoke._write_failure_receipt",
            side_effect=OSError("receipt disk unavailable"),
        ):
            with self.assertRaises(SystemExit) as caught:
                run_cli(["--receipt-out", "/unused/fail.json"])

        self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main()
