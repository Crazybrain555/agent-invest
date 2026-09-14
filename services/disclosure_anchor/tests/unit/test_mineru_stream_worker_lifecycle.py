"""Real worker pressure context/journal ownership; no DB or network execution."""

from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import mineru_stream_worker as owned
from disclosure_anchor.adapters.runtime.mineru_pressure_journal import PressureJournal
from disclosure_anchor.adapters.runtime.mineru_stream_activation import (
    LoadedMineruStreamActivation,
)
from disclosure_anchor.adapters.runtime.mineru_stream_pressure import (
    StreamPressureCache,
)
from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPTransportError,
    BoundedHTTPProtocolError,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorSnapshot,
    CoordinatorTerminal,
    ResourceCreditVector,
)
from disclosure_anchor.cli import worker as worker_cli
from tests.unit import test_mineru_stream_pressure_adapter as readers_fixture
from tests.unit.test_mineru_stream_policy import config


class PressureJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pressure-journal-independent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_private_bounded_segments_keep_complete_JSON_lines_and_leave_other_owners_alone(
        self,
    ):
        other = PressureJournal(self.root)
        self.addCleanup(other.close)
        other({"other_owner": True})
        journal = PressureJournal(self.root, segment_bytes=262144, segments=2)
        self.addCleanup(journal.close)
        self.assertNotEqual(journal.path, other.path)
        self.assertEqual(stat.S_IMODE(journal.path.stat().st_mode), 0o700)
        for index in range(6):
            journal({"index": index, "payload": "x" * 150000})
            files = sorted(journal.path.glob("*.jsonl"))
            self.assertLessEqual(len(files), 2)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 2 * 262144)
            for path in files:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertTrue(path.read_bytes().endswith(b"\n"))
                for line in path.read_bytes().splitlines():
                    self.assertIsInstance(json.loads(line), dict)
        self.assertEqual(
            [
                json.loads(path.read_bytes())["index"]
                for path in sorted(journal.path.glob("*.jsonl"))
            ],
            [4, 5],
        )
        self.assertEqual(
            json.loads(next(other.path.glob("*.jsonl")).read_bytes()),
            {"other_owner": True},
        )
        journal.close()
        with self.assertRaises(RuntimeError):
            journal({"late": True})
        journal.close()

    def test_oversized_nonfinite_and_write_failures_stay_visible_without_silent_records(
        self,
    ):
        journal = PressureJournal(self.root)
        self.addCleanup(journal.close)
        for event in ({"large": "x" * 262144}, {"nan": float("nan")}):
            with self.assertRaises(ValueError):
                journal(event)
        self.assertEqual(next(journal.path.glob("*.jsonl")).stat().st_size, 0)
        marker = OSError("local evidence write failed")
        with (
            mock.patch.object(journal._file, "write", side_effect=marker),
            self.assertRaises(OSError) as caught,
        ):
            journal({"event": "valid"})
        self.assertIs(caught.exception, marker)
        journal.close()
        self.assertTrue(journal._file.closed)


class OwnedStreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pressure-worker-independent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        binding, _, _ = readers_fixture.examples()
        self.activation = LoadedMineruStreamActivation(
            binding=binding,
            policy=config(
                qualified_max=2,
                runtime_identity_sha256=binding.runtime_identity_sha256,
                owner_identity_sha256=binding.owner_sha256,
                sample_max_age_seconds=8.0,
            ),
            source_path=self.root / "activation.json",
            source_sha256="sha256:" + "4" * 64,
        )
        self.settings = SimpleNamespace(
            disclosure_mineru_stream_pressure_config=self.activation.source_path,
            disclosure_mineru_stream_pressure_config_sha256=self.activation.source_sha256,
            disclosure_mineru_runtime_bundle_identity_sha256=binding.runtime_identity_sha256,
            disclosure_mineru_api_url="http://api.invalid",
            disclosure_gpu_metrics_url="http://gpu.invalid/metrics",
            disclosure_runtime_root=self.root,
            worker_loop_interval_seconds=1,
        )
        self.events, self.journals, self.readers = [], [], []
        self.fail_phase = None
        self.marker = RuntimeError("injected lifecycle failure")

    def patched_ownership(self):
        case = self

        class Journal(PressureJournal):
            def __init__(self, parent):
                case.events.append("journal.open")
                super().__init__(parent)
                case.journals.append(self)

            def close(self):
                case.events.append("journal.close")
                super().close()

        class Readers:
            def __init__(self, binding, **kwargs):
                case.events.append("readers.construct")
                self.cache = StreamPressureCache(binding)
                self.active = False
                case.readers.append(self)
                case.assertIs(binding, case.activation.binding)
                case.assertIs(kwargs["evidence_sink"], case.journals[-1])

            def start(self):
                case.events.append("readers.start")
                self.active = True

            def wait_initial_sample(self):
                case.events.append("readers.qualified")
                if case.fail_phase == "qualification":
                    raise case.marker

            def close(self):
                case.events.append("readers.close")
                self.active = False
                if case.fail_phase == "reader.close":
                    raise case.marker

        def load(*args, **kwargs):
            self.events.append("activation.load")
            self.assertIs(kwargs["expected_capacity"], self.activation.binding.capacity)
            return self.activation

        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(owned, "load_mineru_stream_activation", side_effect=load)
        )
        stack.enter_context(mock.patch.object(owned, "PressureJournal", Journal))
        stack.enter_context(mock.patch.object(owned, "StreamPressureSession", Readers))
        return stack

    def test_disabled_context_yields_none_without_starting_readers_or_journal(self):
        self.settings.disclosure_mineru_stream_pressure_config = None
        self.settings.disclosure_mineru_stream_pressure_config_sha256 = None
        with (
            mock.patch.object(owned, "PressureJournal") as journal,
            mock.patch.object(owned, "StreamPressureSession") as readers,
        ):
            with owned.owned_mineru_stream_control(
                self.settings, expected_capacity=None, wakeup=mock.Mock()
            ) as selected:
                self.assertIsNone(selected)
        journal.assert_not_called()
        readers.assert_not_called()

    def test_initial_qualification_failure_closes_registered_readers_then_journal(self):
        self.fail_phase = "qualification"
        with self.patched_ownership(), self.assertRaises(RuntimeError) as caught:
            with owned.owned_mineru_stream_control(
                self.settings,
                expected_capacity=self.activation.binding.capacity,
                wakeup=lambda: None,
            ):
                self.fail("invalid initial pressure must fail before work starts")
        self.assertIs(caught.exception, self.marker)
        self.assertEqual(self.events[-2:], ["readers.close", "journal.close"])
        self.assertFalse(self.readers[0].active)
        self.assertTrue(self.journals[0]._file.closed)

    def test_actual_resident_scope_keeps_same_control_until_runtime_close_on_all_paths(
        self,
    ):
        for phase in (
            None,
            "builder",
            "verify",
            "run",
            "runtime.close",
            "reader.close",
        ):
            self.events.clear()
            self.readers.clear()
            self.journals.clear()
            self.fail_phase = phase
            stop = threading.Event()
            captured = {}

            def check_active():
                self.assertTrue(self.readers[0].active)
                self.assertFalse(self.journals[0]._file.closed)

            def verify():
                check_active()
                self.events.append("runtime.verify")
                if phase == "verify":
                    raise self.marker

            def run(**kwargs):
                check_active()
                self.events.append("runtime.run")
                captured["progress"](
                    CoordinatorSnapshot(
                        admission_open=False,
                        recovery_complete=True,
                        circuit_open=False,
                        queued=(),
                        in_flight=(),
                        credits_in_use=ResourceCreditVector(),
                        credits_limit=ResourceCreditVector(),
                        completed=0,
                        blocked_reason="memory_pause",
                        credit_blocked_by_lane=(),
                        stream_target=0,
                        stream_actual=2,
                        stream_reason="memory_pause",
                        stream_evidence_sha256="sha256:" + "5" * 64,
                    )
                )
                if phase == "run":
                    raise self.marker
                stop.set()
                return SimpleNamespace(
                    terminal=CoordinatorTerminal.QUIESCENT, errors=()
                )

            def close_runtime():
                check_active()
                self.events.append("runtime.close")
                if phase == "runtime.close":
                    raise self.marker

            runtime = SimpleNamespace(
                verify_startup=verify,
                coordinator=SimpleNamespace(run=run),
                close=close_runtime,
            )

            def build(**kwargs):
                captured.update(kwargs)
                self.events.append("builder")
                check_active()
                self.assertIs(kwargs["stream_control"].pressure, self.readers[0].cache)
                self.assertIs(
                    kwargs["expected_capacity"], self.activation.binding.capacity
                )
                if phase == "builder":
                    raise self.marker
                return runtime

            out = io.StringIO()
            with (
                self.subTest(phase=phase),
                self.patched_ownership(),
                mock.patch(
                    "disclosure_anchor.adapters.runtime.staged_worker_v4.build_staged_worker_v4_runtime",
                    side_effect=build,
                ),
                redirect_stdout(out),
            ):

                def invoke():
                    worker_cli._run_staged_v4_resident(
                        self.settings,
                        engine=object(),
                        deps=SimpleNamespace(
                            config=SimpleNamespace(
                                process_scope_classes=("annual_report",)
                            ),
                            heartbeat=lambda: None,
                        ),
                        should_stop=stop.is_set,
                        ownership_guard=lambda: None,
                        admission_guard=lambda: None,
                        work_available=threading.Event(),
                        prune_tracker=worker_cli._ProjectionPruneTracker(),
                        progress_output="jsonl",
                        expected_capacity=self.activation.binding.capacity,
                    )

                if phase is None:
                    invoke()
                else:
                    with self.assertRaises(RuntimeError) as caught:
                        invoke()
                    self.assertIs(caught.exception, self.marker)
            self.assertEqual(self.events[-2:], ["readers.close", "journal.close"])
            self.assertTrue(self.journals[0]._file.closed)
            self.assertFalse(self.readers[0].active)
            self.assertLess(
                self.events.index("readers.qualified"), self.events.index("builder")
            )
            if phase != "builder":
                self.assertLess(
                    self.events.index("runtime.close"),
                    self.events.index("readers.close"),
                )
            if phase in (None, "run", "runtime.close", "reader.close"):
                report = json.loads(out.getvalue())
                self.assertEqual(
                    (report["stream_target"], report["stream_actual"]), (0, 2)
                )
                self.assertEqual(report["stream_reason"], "memory_pause")


class InitialSampleQualificationTests(unittest.TestCase):
    def fixture(self):
        fixture = readers_fixture.PressureSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_two_real_local_reader_threads_qualify_known_low_frame_before_policy_pauses(
        self,
    ):
        fixture = self.fixture()
        session = fixture.session()
        session.start()
        try:
            session.wait_initial_sample(timeout_seconds=0.5)
            sample = session.cache.latest()
            self.assertIsNone(sample.unknown_reason)
            self.assertEqual(sample.host_available_bytes, 700)
        finally:
            session.close()
        self.assertTrue(all(client.closed for client in fixture.clients))

    def test_missing_lane_times_out_and_protocol_fatal_fails_promptly_without_claiming(
        self,
    ):
        for fatal in (False, True):
            fixture = self.fixture()
            error = (
                BoundedHTTPProtocolError("invalid identity/frame")
                if fatal
                else BoundedHTTPTransportError("source unavailable")
            )

            def fail(_):
                raise error

            fixture.actions["api"] = fail
            session = fixture.session()
            session.start()
            began = time.monotonic()
            with (
                self.subTest(fatal=fatal),
                self.assertRaisesRegex(
                    RuntimeError,
                    "startup failed"
                    if fatal
                    else "initial pressure sample unavailable",
                ),
            ):
                session.wait_initial_sample(timeout_seconds=0.04)
            self.assertLess(time.monotonic() - began, 0.5)
            if fatal:
                with self.assertRaises(BaseExceptionGroup):
                    session.close()
            else:
                session.close()
            self.assertTrue(all(client.closed for client in fixture.clients))
