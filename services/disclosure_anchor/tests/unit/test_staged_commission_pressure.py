"""Finite commissioning uses the real owned context; no database or network IO."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import mineru_stream_worker as owned
from disclosure_anchor.adapters.runtime.mineru_pressure_journal import PressureJournal
from disclosure_anchor.adapters.runtime.mineru_stream_activation import LoadedMineruStreamActivation
from disclosure_anchor.adapters.runtime.mineru_stream_pressure import StreamPressureCache
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.services.mineru_stream_policy import StreamAdmissionControl
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorResult, CoordinatorTerminal
from disclosure_anchor.cli import staged_commission as commission
from tests.unit import test_mineru_stream_pressure_adapter as pressure_fixture
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_mineru_stream_policy import config


class StagedCommissionPressureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="commission-pressure-independent-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        binding, _, _ = pressure_fixture.examples()
        self.activation = LoadedMineruStreamActivation(
            binding=binding,
            policy=config(qualified_max=2,
                          runtime_identity_sha256=binding.runtime_identity_sha256,
                          owner_identity_sha256=binding.owner_sha256,
                          sample_max_age_seconds=8.0),
            source_path=root / "activation.json", source_sha256="sha256:" + "4" * 64,
        )
        self.settings = SimpleNamespace(
            worker_parse_execution_mode="staged-v4",
            disclosure_mineru_stream_pressure_config=self.activation.source_path,
            disclosure_mineru_stream_pressure_config_sha256=self.activation.source_sha256,
            disclosure_mineru_runtime_bundle_identity_sha256=binding.runtime_identity_sha256,
            disclosure_runtime_root=root, disclosure_mineru_api_url="http://api.invalid",
            disclosure_gpu_metrics_url="http://gpu.invalid/metrics",
        )
        self.documents = ("chosen-1", "chosen-2")
        self.events, self.readers, self.journals = [], [], []
        self.phase = None
        self.marker = RuntimeError("independent lifecycle marker")
        self.captured = {}

    def _raise_at(self, name):
        self.events.append(name)
        if self.phase == name:
            raise self.marker

    def _patch_boundaries(self, *, enabled=True):
        case = self
        stack = ExitStack()

        class Journal(PressureJournal):
            def __init__(self, parent):
                case.events.append("journal.open")
                super().__init__(parent)
                case.journals.append(self)

            def close(self):
                case.events.append("journal.close")
                super().close()

        class Readers:
            def __init__(self, binding, *, evidence_sink, wakeup, **kwargs):
                self.active = False
                self.cache = StreamPressureCache(binding)
                case.assertIs(binding, case.activation.binding)
                case.assertIs(evidence_sink, case.journals[-1])
                case.assertTrue(callable(wakeup))
                self.wakeup = wakeup
                case.readers.append(self)

            def start(self):
                case.events.append("reader.start")
                self.active = True
                self.wakeup()

            def wait_initial_sample(self):
                case._raise_at("reader.initial")

            def close(self):
                self.active = False
                case._raise_at("reader.close")

        def activation(*args, **kwargs):
            self.events.append("activation.load")
            self.assertIs(kwargs["expected_capacity"], self.activation.binding.capacity)
            return self.activation

        if enabled:
            stack.enter_context(mock.patch.object(owned, "load_mineru_stream_activation", side_effect=activation))
        else:
            self.settings.disclosure_mineru_stream_pressure_config = None
            self.settings.disclosure_mineru_stream_pressure_config_sha256 = None
        self.reader_factory = stack.enter_context(mock.patch.object(owned, "StreamPressureSession", side_effect=Readers))
        self.journal_factory = stack.enter_context(mock.patch.object(owned, "PressureJournal", side_effect=Journal))
        prefix = "disclosure_anchor.cli.staged_commission."
        stack.enter_context(mock.patch(prefix + "_load_staged_process_profile", return_value=SimpleNamespace(profile=_profile())))
        self.checker = stack.enter_context(mock.patch(prefix + "MinerUDeploymentChecker")).return_value
        self.checker.expected_capacity = self.activation.binding.capacity
        self.lock_engine = stack.enter_context(mock.patch(prefix + "sa.create_engine")).return_value
        self.lock_conn = self.lock_engine.connect.return_value.__enter__.return_value
        self.engine = stack.enter_context(mock.patch(prefix + "_create_worker_db_engine")).return_value
        self.engine.dispose.side_effect = lambda: self.events.append("engine.dispose")
        self.lock_engine.dispose.side_effect = lambda: self.events.append("lock.dispose")
        stack.enter_context(mock.patch(prefix + "_database_url", return_value="unused"))
        stack.enter_context(mock.patch(prefix + "require_runtime_app_connection", side_effect=lambda _: self._raise_at("db.connection")))
        stack.enter_context(mock.patch(prefix + "require_runtime_app_engine", side_effect=lambda _: self._raise_at("db.engine")))

        def lock(*args):
            self.events.append("singleton")
            return SimpleNamespace(scalar_one=lambda: self.phase != "singleton")

        self.lock_conn.execute.side_effect = lock
        self.singleton_guard = stack.enter_context(mock.patch(prefix + "_assert_staged_singleton"))
        self.admission_guard = stack.enter_context(mock.patch(prefix + "_assert_worker_admission"))
        stack.enter_context(mock.patch(prefix + "_process_scope_classes", return_value=("annual_report",)))
        before = {key: {"status": "registered", "current_processing_run_id": None} for key in self.documents}
        after = {key: {"status": "published", "current_processing_run_id": "run-" + key,
                      "attempt_run_id": "run-" + key, "attempt_state": "acked",
                      "run_is_active": True, "run_status": "succeeded"} for key in self.documents}
        document_reads = iter((before, after))

        def documents(engine, selected):
            self.assertIs(engine, self.engine)
            self.assertEqual(selected, self.documents)
            self.events.append("documents")
            return {} if self.phase == "missing.document" else next(document_reads)

        stack.enter_context(mock.patch(prefix + "_documents", side_effect=documents))
        self.runtime = mock.Mock(owner_identity="test-owner", worker_profile_sha256="sha256:" + "f" * 64)

        def check_reader():
            if enabled:
                self.assertEqual(len(self.readers), 1, "runtime was reached before pressure readers started")
                self.assertTrue(self.readers[0].active)
                self.assertFalse(self.journals[0]._file.closed)

        def verify():
            check_reader()
            self._raise_at("runtime.verify")

        def run(**kwargs):
            check_reader()
            self._raise_at("runtime.run")
            self.assertFalse(kwargs["stop_requested"]())
            return CoordinatorResult(CoordinatorTerminal.QUIESCENT, True, 2, 2, (), (), ResourceCreditVector())

        def close():
            check_reader()
            self._raise_at("runtime.close")

        self.runtime.verify_startup.side_effect = verify
        self.runtime.coordinator.run.side_effect = run
        self.runtime.close.side_effect = close

        def build(**kwargs):
            self.captured.update(kwargs)
            self._raise_at("runtime.build")
            check_reader()
            kwargs["ownership_guard"]()
            kwargs["admission_guard"]()
            return self.runtime

        self.builder = stack.enter_context(mock.patch(prefix + "build_staged_worker_v4_runtime", side_effect=build))
        return stack

    def _run(self):
        return commission.run_commissioning(self.settings, document_ids=self.documents, max_seconds=5)

    def _closed(self, *, runtime_built=True):
        self.assertEqual(len(self.readers), 1)
        self.assertFalse(self.readers[0].active)
        self.assertTrue(self.journals[0]._file.closed)
        self.assertEqual(self.events.count("reader.close"), 1)
        self.assertLess(self.events.index("reader.close"), self.events.index("journal.close"))
        self.assertLess(self.events.index("journal.close"), self.events.index("engine.dispose"))
        if runtime_built:
            self.runtime.close.assert_called_once_with()
            self.assertLess(self.events.index("runtime.close"), self.events.index("reader.close"))
        else:
            self.runtime.close.assert_not_called()
        self.engine.dispose.assert_called_once_with()
        self.lock_engine.dispose.assert_called_once_with()

    def test_same_real_control_after_gates_keeps_scope_guards_and_original_receipt(self):
        with self._patch_boundaries():
            result = self._run()
        self.assertIsInstance(self.captured["stream_control"], StreamAdmissionControl)
        self.assertIs(self.captured["stream_control"].pressure, self.readers[0].cache)
        self.assertIs(self.captured["expected_capacity"], self.activation.binding.capacity)
        self.assertEqual(self.captured["admission_document_ids"], self.documents)
        self.assertNotIn("campaign_scope", self.captured)
        self.singleton_guard.assert_called_once_with(self.lock_conn)
        self.admission_guard.assert_called_once_with(self.lock_conn, mineru_checker=self.checker,
                                                     singleton_guard=self.captured["ownership_guard"])
        for gate in ("db.connection", "singleton", "db.engine", "documents"):
            self.assertLess(self.events.index(gate), self.events.index("reader.start"))
        self.assertLess(self.events.index("reader.initial"), self.events.index("runtime.build"))
        self.assertEqual(result["contract_version"], "staged-v4-commissioning.v1")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual([item["document_id"] for item in result["documents"]], list(self.documents))
        self.runtime.coordinator.run.assert_called_once()
        self._closed()

    def test_default_off_uses_real_absent_loader_without_readers_or_journal(self):
        with self._patch_boundaries(enabled=False):
            self.assertEqual(self._run()["result"], "PASS")
        self.assertIsNone(self.captured.get("stream_control"))
        self.reader_factory.assert_not_called()
        self.journal_factory.assert_not_called()
        self.runtime.close.assert_called_once_with()
        self.engine.dispose.assert_called_once_with()
        self.lock_engine.dispose.assert_called_once_with()

    def test_initial_reader_failure_prevents_runtime_construction_and_closes_engines(self):
        self.phase = "reader.initial"
        with self._patch_boundaries(), self.assertRaises(RuntimeError) as caught:
            self._run()
        self.assertIs(caught.exception, self.marker)
        self.builder.assert_not_called()
        self.runtime.coordinator.run.assert_not_called()
        self._closed(runtime_built=False)

    def test_runtime_and_cancellation_failures_keep_reader_until_runtime_close(self):
        for phase in ("runtime.build", "runtime.verify", "runtime.run", "runtime.close", "reader.close", "cancel"):
            with self.subTest(phase=phase):
                self.events.clear()
                self.readers.clear()
                self.journals.clear()
                self.phase = "runtime.run" if phase == "cancel" else phase
                self.marker = KeyboardInterrupt("independent cancellation") if phase == "cancel" else RuntimeError(phase)
                with self._patch_boundaries(), self.assertRaises(type(self.marker)) as caught:
                    self._run()
                self.assertIs(caught.exception, self.marker)
                self._closed(runtime_built=phase != "runtime.build")
                if phase in {"runtime.build", "runtime.verify"}:
                    self.runtime.coordinator.run.assert_not_called()

    def test_preconditions_and_finite_scope_reject_before_starting_pressure(self):
        for phase in ("db.connection", "singleton", "db.engine", "missing.document", "none.scope", "nine.scope"):
            with self.subTest(phase=phase):
                self.events.clear()
                self.phase = phase
                with self._patch_boundaries(), self.assertRaises((ValueError, RuntimeError)):
                    if phase == "none.scope":
                        commission.run_commissioning(self.settings, document_ids=None, max_seconds=5)
                    elif phase == "nine.scope":
                        commission.run_commissioning(self.settings, document_ids=tuple(str(i) for i in range(9)), max_seconds=5)
                    else:
                        self._run()
                self.reader_factory.assert_not_called()
                self.journal_factory.assert_not_called()
                self.builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
