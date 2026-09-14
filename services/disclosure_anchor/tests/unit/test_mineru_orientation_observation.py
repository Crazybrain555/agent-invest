"""Independent caller regressions against SHA-pinned, patched MinerU sources.

No GPU/model imports: generated function bodies are unchanged. Only external
image/model compute and the proc/output boundaries are deterministic sentinels.
Set M6_TEST_SERVICE_ROOT and optionally M6_TEST_PATCHER for an isolated candidate.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import ExitStack, contextmanager, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SERVICE = Path(os.environ.get("M6_TEST_SERVICE_ROOT", Path(__file__).parents[2]))
PATCHER = Path(os.environ.get(
    "M6_TEST_PATCHER", SERVICE / "scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py",
))
PREIMAGES = {
    "mineru/backend/hybrid/hybrid_analyze.py":
        "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0",
    "mineru/utils/model_utils.py":
        "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109",
}
MODEL_REL = "mineru/utils/model_utils.py"
HYBRID_REL = "mineru/backend/hybrid/hybrid_analyze.py"
PROC = "101 (fixture with ) parentheses) " + " ".join(["S"] + ["0"] * 18 + ["987654"])
BUSINESS_WARNING = "Hybrid medium effort table orientation classification failed:"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generated_sources():
    patcher = load_file("independent_orientation_patcher", PATCHER)
    result = {}
    for relative, expected in PREIMAGES.items():
        raw = (SERVICE / "tests/fixtures/mineru_344_preimages" / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise AssertionError(f"official preimage drift: {relative}")
        result[relative] = patcher.patch_source(relative, raw.decode("utf-8"))
        compile(result[relative], relative, "exec")
    return result


def exec_nodes(nodes, filename, namespace):
    unit = ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), filename, "exec"), namespace)


class MetadataNet:
    """Weak-referenceable serving object; no tensor values or transfers exist."""

    def parameters(self):
        return iter([SimpleNamespace(device="cuda:0", dtype="torch.float16", numel=lambda: 6)])

    def buffers(self):
        return iter([])


class OrientationModel:
    def __init__(self):
        self.ocr_engine = SimpleNamespace(**{
            name: SimpleNamespace(net=MetadataNet(), weights_path=f"/weights/fixture/{name}")
            for name in ("text_detector", "text_recognizer")
        })
        self.batch_predict = Mock(return_value=["90"])


class OrientationFixture:
    def __init__(self, sources):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"MINERU_PHASE_TRACE": "1"}, clear=True))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stderr(self.output))
        self.logs = []
        self.logger = SimpleNamespace(**{
            name: lambda *args, **kwargs: self.logs.append(" ".join(map(str, args)))
            for name in ("warning", "error", "exception", "info", "debug")
        })
        self.model_module = ModuleType("independent_generated_orientation_model")
        self.stack.enter_context(patch.dict(sys.modules, {self.model_module.__name__: self.model_module}))
        self.ns = vars(self.model_module)
        self.ns["logger"] = self.logger
        allowed = {
            "asyncio", "ctypes", "dataclasses", "functools", "hashlib", "json", "math",
            "os", "stat", "sys", "threading", "time", "uuid", "weakref", "gc",
            "logging", "warnings", "contextlib", "enum",
        }
        nodes = []
        for node in ast.parse(sources[MODEL_REL]).body:
            if isinstance(node, ast.Import) and all(a.name in allowed for a in node.names):
                nodes.append(node)
            elif isinstance(node, ast.ImportFrom) and node.module in allowed:
                nodes.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                   ast.Assign, ast.AnnAssign)):
                nodes.append(node)
        # Startup validation and optional native imports are excluded; the real
        # bootstrap is exercised below after loading the helper definitions.
        exec_nodes(nodes, "<exact-generated-model-utils>", self.ns)
        self.ns["open"] = self.proc_open
        self.model = OrientationModel()
        self.owner = SimpleNamespace(atom_model_manager=SimpleNamespace(
            get_atom_model=Mock(return_value=self.model)), lang="ch")
        self.gate = Mock(side_effect=lambda function, *args, **kwargs: function(*args, **kwargs))
        self.consumer = {
            "logger": self.logger, "AtomicModel": SimpleNamespace(TableOrientationCls="orientation"),
            "OCR_DET_BASE_BATCH_SIZE": 8, "run_ocr_inference": self.gate,
        }
        tree = ast.parse(sources[HYBRID_REL])
        for node in tree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and all(isinstance(target, ast.Name) for target in node.targets)):
                exec_nodes([node], "<exact-generated-hybrid-constant>", self.consumer)
            if isinstance(node, ast.ImportFrom) and node.module == "mineru.utils.model_utils":
                for alias in node.names:
                    self.consumer[alias.asname or alias.name] = self.ns[alias.name]
        # Match the actual generated import; never substitute a no-op recorder.
        if self.consumer.get("record_hybrid_model_devices") is not self.ns["record_hybrid_model_devices"]:
            raise AssertionError("generated caller must import the real recorder")
        exec_nodes([node for node in tree.body if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef),
        )], "<exact-generated-hybrid>", self.consumer)
        self.consumer["crop_img"] = Mock(return_value=(object(), None))
        self.layout = {"label": "table", "bbox": [0.1, 0.2, 0.9, 0.8], "angle": "0"}

    @staticmethod
    def proc_open(path, *args, **kwargs):
        if path != "/proc/self/stat":
            raise AssertionError(f"unexpected model evidence read: {path}")
        return io.StringIO(PROC)

    def call(self):
        return self.consumer["_apply_medium_table_orientation_labels"](
            [SimpleNamespace(size=(100, 200))], [[self.layout]], self.owner, batch_ratio=2,
        )

    @contextmanager
    def capacity(self):
        module = load_file("orientation_real_capacity_fixture", SERVICE / "tests/_mineru_capacity_bootstrap_fixture.py")
        with tempfile.TemporaryDirectory(prefix="mineru-orientation-capacity-") as temporary:
            root = Path(temporary).resolve()
            fixture = module.CapacityBootstrapFixture(root)
            try:
                payload = {
                    "api_event_loop_limit": 1, "api_process_limit": 1,
                    "contract_version": "mineru.capacity-config.v1", "final_http_limit_per_loop": 7,
                    "finalizer_active_limit": 1, "hybrid_batch_ratio_requested": 1,
                    "max_unacked_result_bytes": 2097152, "mkl_num_threads": 4,
                    "omp_num_threads": 4, "openblas_num_threads": 1, "parse_active_limit": 5,
                    "pdf_render_processes_requested": 3, "pipeline_inference_locks": True,
                    "processing_window_size": 16, "result_reservation_bytes": 262144,
                    "total_nonterminal_limit": 6,
                }
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                config = fixture.codec.decode_mineru_capacity_config(raw)
                path = root / "capacity.json"
                path.write_bytes(raw)
                path.chmod(0o600)
                environment = fixture.bootstrap.capacity_environment(config)
                environment.update({"MINERU_PHASE_TRACE": "1", "MINERU_CAPACITY_CONFIG_PATH": str(path),
                                    "MINERU_CAPACITY_CONFIG_SHA256": "sha256:" + hashlib.sha256(raw).hexdigest()})
                with patch.dict(os.environ, environment, clear=True):
                    yield fixture.bootstrap
            finally:
                fixture.close()

    def close(self):
        self.stack.close()


class OrientationObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = generated_sources()

    def setUp(self):
        self.fx = OrientationFixture(self.sources)
        self.addCleanup(self.fx.close)

    def assert_classified(self, calls=1):
        self.assertEqual(self.fx.model.batch_predict.call_count, calls)
        self.assertEqual(self.fx.layout["angle"], "90")
        self.assertEqual(self.fx.gate.call_count, calls)
        self.assertFalse(any(BUSINESS_WARNING in line for line in self.fx.logs), self.fx.logs)

    def assert_observation_visible(self, message):
        self.assertIn(message, "\n".join(self.fx.logs) + self.fx.output.getvalue())

    def test_success_uses_serving_model_and_original_batch_arguments(self):
        self.fx.call()
        self.assert_classified()
        self.fx.owner.atom_model_manager.get_atom_model.assert_called_once_with(
            atom_model_name="orientation", lang="ch",
        )
        self.assertEqual(self.fx.model.batch_predict.call_args.kwargs,
                         {"det_batch_size": 16, "tqdm_enable": True})
        events = [json.loads(line.split(" ", 1)[1]) for line in self.fx.output.getvalue().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["role"], "orientation")
        self.assertEqual(events[0]["process_start_ticks"], 987654)

    def test_observational_proc_oserror_visible_classification_continues_and_retry_emits(self):
        with patch.dict(self.fx.ns, {"open": Mock(side_effect=OSError("fixture proc read failed"))}):
            self.fx.call()
        self.assert_classified()
        self.assert_observation_visible("fixture proc read failed")
        self.assertNotIn("MINERU_MODEL_DEVICE ", self.fx.output.getvalue())
        self.fx.call()
        self.assert_classified(calls=2)
        self.assertEqual(self.fx.output.getvalue().count("MINERU_MODEL_DEVICE "), 1)

    def test_observational_stderr_oserror_visible_and_classification_continues(self):
        output = SimpleNamespace(write=Mock(side_effect=OSError("fixture evidence output failed")),
                                 flush=Mock())
        with redirect_stderr(output):
            self.fx.call()
        self.assert_classified()
        self.assert_observation_visible("fixture evidence output failed")
        self.fx.call()
        self.assert_classified(calls=2)
        self.assertEqual(self.fx.output.getvalue().count("MINERU_MODEL_DEVICE "), 1)

    def test_verified_capacity_identity_drift_propagates_before_classifier(self):
        with self.fx.capacity() as bootstrap:
            bootstrap.get_process_capacity()
            with patch.dict(os.environ, {"OMP_NUM_THREADS": "99"}):
                with self.assertRaisesRegex(RuntimeError, "drifted"):
                    self.fx.call()
            self.fx.call()
        self.assert_classified()

    def test_partial_capacity_identity_propagates_before_classifier(self):
        with self.fx.capacity():
            del os.environ["MINERU_CAPACITY_CONFIG_SHA256"]
            with self.assertRaisesRegex(ValueError, "path and SHA must both be explicit"):
                self.fx.call()
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.layout["angle"], "0")
        self.assertEqual(self.fx.logs, [])

    def test_capacity_file_oserror_is_authoritative_and_propagates(self):
        with self.fx.capacity():
            Path(os.environ["MINERU_CAPACITY_CONFIG_PATH"]).unlink()
            with self.assertRaises(FileNotFoundError):
                self.fx.call()
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.logs, [])

    def test_seen_model_does_not_bypass_later_capacity_validation(self):
        with self.fx.capacity():
            self.fx.call()
            self.assert_classified()
            with patch.dict(os.environ, {"OMP_NUM_THREADS": "99"}):
                with self.assertRaisesRegex(RuntimeError, "drifted"):
                    self.fx.call()
            self.assert_classified()

    def test_unknown_metadata_error_propagates_without_business_fallback(self):
        failure = RuntimeError("fixture metadata getter failure")
        self.fx.model.ocr_engine.text_detector.net.parameters = Mock(side_effect=failure)
        with self.assertRaises(RuntimeError) as raised:
            self.fx.call()
        self.assertIs(raised.exception, failure)
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.logs, [])

    def test_malformed_process_identity_propagates_without_business_fallback(self):
        malformed = PROC.replace("987654", "not-an-integer")
        with patch.dict(self.fx.ns, {"open": lambda *_args, **_kwargs: io.StringIO(malformed)}):
            with self.assertRaises(ValueError):
                self.fx.call()
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.logs, [])

    def test_invalid_trace_policy_propagates_before_classifier(self):
        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "invalid"}):
            with self.assertRaisesRegex(RuntimeError, "MINERU_PHASE_TRACE"):
                self.fx.call()
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.logs, [])

    def test_observation_cancellation_and_baseexceptions_propagate(self):
        for error in (asyncio.CancelledError("cancel observation"), KeyboardInterrupt("interrupt observation"),
                      SystemExit("stop observation")):
            with self.subTest(error=type(error).__name__):
                with patch.dict(self.fx.ns, {"open": Mock(side_effect=error)}):
                    with self.assertRaises(type(error)) as raised:
                        self.fx.call()
                self.assertIs(raised.exception, error)
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.logs, [])

    def test_original_classification_error_keeps_original_angle_and_warning(self):
        self.fx.model.batch_predict.side_effect = RuntimeError("fixture classifier failed")
        self.fx.call()
        self.fx.model.batch_predict.assert_called_once()
        self.assertEqual(self.fx.layout["angle"], "0")
        self.assertEqual(self.fx.logs, [BUSINESS_WARNING +
            " fixture classifier failed, using original table images"])

    def test_original_prediction_count_mismatch_keeps_original_angle(self):
        self.fx.model.batch_predict.return_value = []
        self.fx.call()
        self.assertEqual(self.fx.layout["angle"], "0")
        self.assertEqual(self.fx.logs, [BUSINESS_WARNING +
            " Table orientation prediction result count mismatch, using original table images"])

    def test_original_model_acquisition_failure_keeps_fallback(self):
        self.fx.owner.atom_model_manager.get_atom_model.side_effect = RuntimeError("fixture model unavailable")
        self.fx.call()
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.layout["angle"], "0")
        self.assertEqual(self.fx.logs, [BUSINESS_WARNING +
            " fixture model unavailable, using original table images"])

    def test_classifier_cancellation_propagates(self):
        error = asyncio.CancelledError("cancel classifier")
        self.fx.model.batch_predict.side_effect = error
        with self.assertRaises(asyncio.CancelledError) as raised:
            self.fx.call()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.fx.logs, [])

    def test_no_tables_does_not_acquire_model_or_read_evidence(self):
        self.fx.layout["label"] = "text"
        with patch.dict(self.fx.ns, {"open": Mock(side_effect=AssertionError("unexpected observation"))}):
            self.fx.call()
        self.fx.owner.atom_model_manager.get_atom_model.assert_not_called()
        self.fx.model.batch_predict.assert_not_called()
        self.assertEqual(self.fx.output.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
