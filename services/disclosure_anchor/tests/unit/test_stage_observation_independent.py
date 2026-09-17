"""Independent tests: real sink/guard and local child, no database/model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.stage_observation import JsonlStageObserver
from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.application.ports.staged_execution import StageNote, current_semantic_group, semantic_group_scope
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost


def guard(observer=None):
    return StageLeaseGuard(time.monotonic()+20, threading.Event(), time.monotonic,
                           attempt_id='attempt-test', lane='commit', observer=observer)


class Collector:
    def __init__(self):
        self.records = []
        self.failures = []
    def note(self, record):
        self.records.append(record)
    def record_failure(self, error):
        self.failures.append(type(error).__name__)


class ObservationSinkTests(unittest.TestCase):
    def test_real_writer_drains_every_accepted_tail_and_joins(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = {"source": "python.time.monotonic_ns", "implementation": "test-monotonic",
                     "boot_session_uuid": "11111111-1111-4111-8111-111111111111"}
            sink = JsonlStageObserver(Path(tmp), max_events=128, max_bytes=65536, clock_binding=clock)
            live = guard(sink)
            for i in range(50):
                live.note('units_built', units=i)
            summary = sink.close()
            rows = [json.loads(line) for line in (Path(tmp)/'stage-events.jsonl').read_text().splitlines()]
            self.assertEqual([row['scalars']['units'] for row in rows[:-1]], list(range(50)))
            self.assertEqual(rows[-1]['kind'], 'observation_closed')
            self.assertEqual(summary['events_written'], len(rows))
            self.assertEqual(summary['bytes_written'], (Path(tmp)/'stage-events.jsonl').stat().st_size)
            self.assertEqual(summary['measurement_status'], 'complete')
            self.assertEqual(summary['clock'], clock)
            self.assertFalse(sink._thread.is_alive())  # Actual thread, not mocked join.
            self.assertEqual(json.loads((Path(tmp)/'observation-summary.json').read_text()), summary)

    def test_note_error_cannot_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = JsonlStageObserver(Path(tmp), max_events=8, max_bytes=4096)
            sink.note(object())
            summary = sink.close()
            self.assertEqual(summary['note_errors'], 1)
            self.assertNotEqual(summary['measurement_status'], 'complete')

    def test_guard_failure_cannot_claim_complete_or_revoke_business(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = JsonlStageObserver(Path(tmp), max_events=8, max_bytes=4096)
            live = guard(sink)
            with mock.patch.object(sink, 'note', side_effect=OSError('secret text must not be logged')):
                live.note('units_built', units=1)
            live.checkpoint()
            summary = sink.close()
            self.assertEqual(summary['guard_failures'], 1)
            self.assertNotEqual(summary['measurement_status'], 'complete')
            self.assertNotIn('secret text', (Path(tmp)/'observation-summary.json').read_text())

    def test_over_byte_budget_is_visible_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = JsonlStageObserver(Path(tmp), max_events=128, max_bytes=4096)
            for i in range(60):
                guard(sink).note('units_built', units=i)
            summary = sink.close()
            self.assertGreater(summary['truncated'], 0)
            self.assertTrue(sink.writer_failed)
            self.assertNotEqual(summary['measurement_status'], 'complete')
            self.assertLessEqual((Path(tmp)/'stage-events.jsonl').stat().st_size, 4096)

    def test_queue_full_records_lost_current_item_then_drains_retained_tail(self):
        entered, release = threading.Event(), threading.Event()
        original = JsonlStageObserver._write
        def paused(writer, line):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test release missing')
            original(writer, line)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(JsonlStageObserver, '_write', paused):
            sink = JsonlStageObserver(Path(tmp), max_events=1, max_bytes=4096)
            try:
                guard(sink).note('units_built', units=0)
                self.assertTrue(entered.wait(2))
                guard(sink).note('units_built', units=1)
                guard(sink).note('units_built', units=2)
            finally:
                release.set()
                summary = sink.close()
            rows = [json.loads(line) for line in (Path(tmp)/'stage-events.jsonl').read_text().splitlines()]
            self.assertEqual([r['scalars']['units'] for r in rows if r['kind']=='units_built'], [0,1])
            self.assertEqual(summary['dropped'], 1)
            self.assertEqual(summary['measurement_status'], 'partial')
            self.assertFalse(sink._thread.is_alive())

    def test_existing_event_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'stage-events.jsonl'
            path.write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                JsonlStageObserver(Path(tmp), max_events=8, max_bytes=4096)
            self.assertEqual(path.read_bytes(), b'original')

    def test_observation_never_restores_lost_lease(self):
        seen = Collector()
        live = guard(seen)
        live.revoke()
        live.note('units_built', units=1)
        with self.assertRaises(StageLeaseLost):
            live.checkpoint()
        self.assertEqual(seen.records[0].attempt_id, 'attempt-test')

    def test_scalar_contract_rejects_complex_and_oversized_values(self):
        for value in (True, {}, [], 'x'*257):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                StageNote('a','commit','units_built',1,(('units',value),))


class RealProcessObservationTests(unittest.TestCase):
    def test_local_child_success_nonzero_and_timeout_have_real_pairs(self):
        for script, timeout, expected in (("print(input())", 2, 0), ('import sys;sys.exit(7)',2,7),
                                           ('import time;time.sleep(5)',1,None)):
            with self.subTest(expected=expected):
                seen = Collector()
                with semantic_group_scope('sha256:'+'7'*64):
                    if expected is None:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            codex_cli._run_process(args=[sys.executable,'-c',script], prompt='private-input',
                                env=dict(os.environ), timeout_seconds=timeout, stage_guard=guard(seen))
                    else:
                        result = codex_cli._run_process(args=[sys.executable,'-c',script], prompt='private-input',
                            env=dict(os.environ), timeout_seconds=timeout, stage_guard=guard(seen))
                        self.assertEqual(result.returncode, expected)
                records = [r for r in seen.records if r.kind.startswith('process_')]
                self.assertEqual([r.kind for r in records], ['process_started','process_ended'])
                self.assertEqual(dict(records[0].scalars)['group_hash'], 'sha256:'+'7'*64)
                self.assertNotIn('private-input', repr(records))
                self.assertIsNone(current_semantic_group())

    def test_spawn_failure_does_not_invent_started_child(self):
        seen = Collector()
        with self.assertRaises(FileNotFoundError):
            codex_cli._run_process(args=['/nonexistent/r17-child'], prompt='', env={},
                                   timeout_seconds=1, stage_guard=guard(seen))
        self.assertFalse(any(r.kind=='process_started' for r in seen.records))

class ProgressFailureTests(unittest.TestCase):
    def test_real_progress_write_failure_does_not_escape_publication_callback(self):
        from disclosure_anchor.adapters.runtime.stage_observation import ProgressRecorder
        with tempfile.TemporaryDirectory() as tmp:
            recorder=ProgressRecorder(Path(tmp))
            original=os.write
            def fail_progress(fd,data):
                if fd==recorder._fd:
                    raise OSError('progress write failed')
                return original(fd,data)
            try:
                with mock.patch('disclosure_anchor.adapters.runtime.stage_observation.os.write',side_effect=fail_progress):
                    recorder.prune_signal(False)
            finally:
                recorder.close()

class SinkCloseProtocolTests(unittest.TestCase):
    def test_producer_paused_before_enqueue_cannot_accept_after_close(self):
        from disclosure_anchor.adapters.runtime import stage_observation as module
        encoded, release = threading.Event(), threading.Event()
        original = module._encode

        def pause_payload(value):
            raw = original(value)
            if value.get("kind") == "units_built":
                encoded.set()
                if not release.wait(3):
                    raise RuntimeError("independent producer barrier timed out")
            return raw

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "_encode", pause_payload):
            sink = JsonlStageObserver(Path(tmp), max_events=8, max_bytes=4096)
            producer = threading.Thread(target=lambda: guard(sink).note("units_built", units=1))
            producer.start()
            try:
                self.assertTrue(encoded.wait(1))
                summary = sink.close()
                self.assertFalse(sink._thread.is_alive())
            finally:
                release.set()
                producer.join(timeout=2)
                if sink._thread.is_alive():
                    sink.close()
            self.assertFalse(producer.is_alive())
            self.assertEqual(sink._queue.qsize(), 0, "a stopped writer cannot drain a post-close enqueue")
            self.assertEqual(sink._counts["late_notes"], 1)
            rows = (Path(tmp) / "stage-events.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), summary["events_written"])
            self.assertEqual([json.loads(row)["kind"] for row in rows], ["observation_closed"])

    def test_close_timeout_eventually_releases_writer_after_io_recovers(self):
        from disclosure_anchor.adapters.runtime import stage_observation as module
        entered,release,recovered=threading.Event(),threading.Event(),threading.Event()
        original=JsonlStageObserver._write
        def paused(writer,line):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test release missing')
            original(writer,line)
            recovered.set()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(JsonlStageObserver,'_write',paused):
            sink=JsonlStageObserver(Path(tmp),max_events=1,max_bytes=4096)
            try:
                guard(sink).note('units_built',units=1)
                self.assertTrue(entered.wait(1))
                guard(sink).note('units_built',units=2)
                summary=sink.close(join_timeout_seconds=0.01)
                self.assertEqual(summary['measurement_status'],'invalid')
                release.set()
                self.assertTrue(recovered.wait(1))
                sink._thread.join(timeout=0.2)
                self.assertFalse(sink._thread.is_alive(), 'lost sentinel leaves non-daemon writer awaiting queue forever')
            finally:
                release.set()
                if sink._thread.is_alive():
                    sink._queue.put(module._SENTINEL,timeout=1)
                    sink._thread.join(timeout=1)
                try:
                    os.close(sink._fd)
                except OSError:
                    pass
