"""Progress loss and CLI quality composition, with real local observation files.

No DB, provider, model, runtime configuration, or production service is used.
"""

import contextlib
import io
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import stage_observation
from disclosure_anchor.adapters.runtime.stage_observation import ProgressRecorder
from disclosure_anchor.cli import staged_commission as cli


def read_progress(directory):
    return [json.loads(line) for line in (directory / "progress.jsonl").read_text().splitlines()]


class ProgressMeasurementTests(unittest.TestCase):
    def test_exact_line_limit_retains_all_signals_and_can_be_complete(self):
        for limit in (1, 3):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                recorder = ProgressRecorder(directory, max_lines=limit)
                signals = [index % 2 == 0 for index in range(limit)]
                try:
                    for replaced in signals:
                        recorder.prune_signal(replaced)
                finally:
                    summary = recorder.close()
                self.assertEqual([row["replaced"] for row in read_progress(directory)], signals)
                self.assertEqual(summary["progress_lines"], limit)
                self.assertEqual(summary["progress_dropped"], 0)
                self.assertEqual(summary["progress_write_errors"], 0)
                self.assertEqual(summary["progress_status"], "complete")
                self.assertFalse(recorder.failed)
                self.assertEqual(recorder.close(), summary)

    def test_one_signal_over_limit_is_partial_without_changing_retained_records(self):
        for limit in (1, 3):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                recorder = ProgressRecorder(directory, max_lines=limit)
                try:
                    for _ in range(limit):
                        recorder.prune_signal(False)
                    recorder.prune_signal(True)
                finally:
                    summary = recorder.close()
                retained_bytes = (directory / "progress.jsonl").read_bytes()
                self.assertEqual([row["replaced"] for row in read_progress(directory)], [False] * limit)
                self.assertEqual(summary["progress_lines"], limit)
                self.assertEqual(summary["progress_dropped"], 1)
                self.assertEqual(summary["progress_write_errors"], 0)
                self.assertEqual(recorder.close(), summary, "closing twice must not add loss or I/O errors")
                self.assertEqual((directory / "progress.jsonl").read_bytes(), retained_bytes)
                self.assertEqual(summary["progress_status"], "partial")

    def test_write_error_is_sticky_invalid_even_with_later_dropped_signals(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            recorder = ProgressRecorder(directory, max_lines=3)
            try:
                recorder.prune_signal(False)
                with mock.patch.object(stage_observation.os, "write", side_effect=OSError("test disk failure")):
                    recorder.prune_signal(True)
                recorder.prune_signal(True)
            finally:
                summary = recorder.close()
            self.assertEqual([row["replaced"] for row in read_progress(directory)], [False])
            self.assertEqual(summary["progress_lines"], 1)
            self.assertEqual(summary["progress_write_errors"], 1)
            self.assertEqual(summary["progress_dropped"], 1)
            self.assertTrue(recorder.failed)
            self.assertEqual(summary["progress_failure_types"], ["OSError"])
            self.assertEqual(summary["progress_status"], "invalid")
            self.assertEqual(recorder.close(), summary)

    def test_flush_failure_is_invalid_and_repeated_close_does_not_repeat_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            recorder = ProgressRecorder(directory)
            recorder.prune_signal(False)
            with mock.patch.object(stage_observation.os, "fsync", side_effect=OSError("test flush failure")) as flush:
                first = recorder.close()
                second = recorder.close()
            self.assertEqual(second, first)
            self.assertEqual(flush.call_count, 1)
            self.assertEqual(len(read_progress(directory)), 1)
            self.assertEqual(first["progress_dropped"], 0)
            self.assertEqual(first["progress_write_errors"], 1)
            self.assertEqual(first["progress_status"], "invalid")


class CommissionMeasurementCompositionTests(unittest.TestCase):
    def _arguments(self, directory):
        return [
            "--document-id", "doc-independent-measurement", "--max-seconds", "1",
            "--receipt-out", str(directory / "receipt.json"),
            "--observation-out", str(directory / "observe"),
        ]

    def test_combined_quality_preserves_both_sinks_and_business_exit(self):
        cases = (
            ("complete", "complete", "complete"),
            ("progress_drop", "partial", "partial"),
            ("progress_write_error", "invalid", "invalid"),
            ("observer_loss", "complete", "partial"),
            ("both_losses", "invalid", "invalid"),
        )
        for case, progress_status, measurement_status in cases:
            for outcome, expected_exit in (("PASS", 0), ("NOT_PASS", 1)):
                with self.subTest(case=case, outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary).resolve()
                    business_result = {"result": outcome, "errors": [] if outcome == "PASS" else ["business-failure"]}

                    def build_progress(path):
                        return ProgressRecorder(path, max_lines=1 if case == "progress_drop" else 3)

                    def commissioning(*args, hooks, **kwargs):
                        hooks.publication_committed(False)
                        if case == "progress_drop":
                            hooks.publication_committed(True)
                        if case in {"progress_write_error", "both_losses"}:
                            # Only the synchronous publication callback writes here;
                            # the real observer has no queued records until close.
                            with mock.patch.object(stage_observation.os, "write", side_effect=OSError("test disk failure")):
                                hooks.publication_committed(True)
                            hooks.publication_committed(True)
                        if case in {"observer_loss", "both_losses"}:
                            hooks.stage_observer.record_failure(ValueError("test note loss"))
                        return business_result

                    output = io.StringIO()
                    with (
                        mock.patch.object(cli, "load_settings", return_value=SimpleNamespace(disclosure_runtime_root=directory)),
                        mock.patch.object(cli, "run_commissioning", side_effect=commissioning),
                        mock.patch.object(cli, "ProgressRecorder", side_effect=build_progress),
                        contextlib.redirect_stdout(output),
                    ):
                        actual_exit = cli.main(self._arguments(directory))
                    printed = [json.loads(line) for line in output.getvalue().splitlines()]
                    observation = printed[-1]["observation"]
                    self.assertEqual(actual_exit, expected_exit)
                    self.assertEqual(json.loads((directory / "receipt.json").read_text()), business_result)
                    self.assertEqual(printed[0], business_result)
                    self.assertEqual(observation["closure_errors"], [])
                    self.assertEqual(observation["writer_thread_alive"], False)
                    self.assertEqual(len(read_progress(directory / "observe")), 1)
                    self.assertEqual(observation["progress_status"], progress_status)
                    self.assertEqual(observation["measurement_status"], measurement_status)

    def test_unexpected_progress_close_error_cannot_replace_business_result_or_exception(self):
        for outcome in ("PASS", "NOT_PASS", "exception"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary).resolve()
                original_error = RuntimeError("original commissioning failure")
                original_close = ProgressRecorder.close
                previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
                output = io.StringIO()

                def commissioning(*args, hooks, **kwargs):
                    hooks.publication_committed(False)
                    if outcome == "exception":
                        raise original_error
                    return {"result": outcome}

                def fail_after_releasing_progress(recorder):
                    # Real cleanup still occurs, so the injected boundary error
                    # cannot leak a test descriptor or depend on recorder internals.
                    original_close(recorder)
                    raise OSError("test close failure")

                with (
                    mock.patch.object(cli, "load_settings", return_value=SimpleNamespace(disclosure_runtime_root=directory)),
                    mock.patch.object(cli, "run_commissioning", side_effect=commissioning),
                    mock.patch.object(ProgressRecorder, "close", fail_after_releasing_progress),
                    contextlib.redirect_stdout(output),
                ):
                    if outcome == "exception":
                        with self.assertRaises(RuntimeError) as caught:
                            cli.main(self._arguments(directory))
                        self.assertIs(caught.exception, original_error)
                    else:
                        self.assertEqual(cli.main(self._arguments(directory)), 0 if outcome == "PASS" else 1)
                observation = json.loads(output.getvalue().splitlines()[-1])["observation"]
                self.assertEqual(observation["measurement_status"], "invalid")
                self.assertEqual(observation["closure_errors"], ["progress:OSError"])
                self.assertFalse(observation["writer_thread_alive"])
                self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
                if outcome == "exception":
                    self.assertFalse((directory / "receipt.json").exists())
                else:
                    self.assertEqual(json.loads((directory / "receipt.json").read_text()), {"result": outcome})


if __name__ == "__main__":
    unittest.main()
