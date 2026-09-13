"""Local process-cache and loaded-getter boundaries; no serving/framework startup."""

import builtins
import importlib.util
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._mineru_capacity_bootstrap_fixture import CapacityBootstrapFixture
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES, capacity_payload


PATH_KEY = "MINERU_CAPACITY_CONFIG_PATH"
SHA_KEY = "MINERU_CAPACITY_CONFIG_SHA256"
ENVIRONMENT = {
    "MINERU_API_MAX_CONCURRENT_REQUESTS": "2",
    "MINERU_API_MAX_PENDING_TASKS": "3",
    "MINERU_API_FINALIZER_SLOTS": "1",
    "MINERU_PROCESSING_WINDOW_SIZE": "16",
    "OMP_NUM_THREADS": "4",
    "MKL_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "1",
    "MINERU_PDF_RENDER_THREADS": "3",
    "MINERU_HYBRID_BATCH_RATIO": "2",
    "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS": "1",
    "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": "31",
    "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": "73",
}


def unavailable(reason):
    return {"state": "unavailable", "value": None, "reason": reason}


class MineruCapacityProcessObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="capacity-process-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = CapacityBootstrapFixture(self.root)
        self.addCleanup(self.fixture.close)
        self.bootstrap = self.fixture.bootstrap
        self.config = self.fixture.codec.MineruCapacityConfig(**capacity_payload())
        self.file = self.root / "capacity.json"
        self.file.write_bytes(CAPACITY_BYTES)
        self.file.chmod(0o600)
        self.environment = {
            **ENVIRONMENT,
            PATH_KEY: str(self.file),
            SHA_KEY: self.config.sha256,
        }
        source = (
            Path(__file__).resolve().parents[2]
            / "scripts/windows/mineru_heap_trim_compat/agent_capacity_observation.py"
        )
        target = self.root / "agent_capacity_observation.py"
        target.write_bytes(source.read_bytes())
        spec = importlib.util.spec_from_file_location(
            "independent_capacity_observation", target
        )
        self.observation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.observation)

    def test_concurrent_first_call_reads_real_file_once_and_cached_call_does_not_reopen(
        self,
    ):
        reading, release, second_started = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = self.bootstrap.read_mineru_capacity_file
        results, errors = [], []

        def read(*args, **kwargs):
            reading.set()
            if not release.wait(2):
                raise AssertionError("test did not release original file read")
            return original(*args, **kwargs)

        def worker(started=None):
            try:
                if started is not None:
                    started.set()
                results.append(self.bootstrap.get_process_capacity())
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=worker),
            threading.Thread(target=worker, args=(second_started,)),
        ]
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                self.bootstrap, "read_mineru_capacity_file", side_effect=read
            ) as reads,
        ):
            try:
                threads[0].start()
                self.assertTrue(reading.wait(2))
                threads[1].start()
                self.assertTrue(second_started.wait(2))
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(2)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertIs(results[0], results[1])
            self.assertEqual(results[0].exact_bytes, CAPACITY_BYTES)
            self.file.unlink()
            self.assertIs(self.bootstrap.get_process_capacity(), results[0])
            self.assertEqual(reads.call_count, 1)

    def test_each_bound_environment_or_anchor_drift_is_rejected_without_read_or_rebind(
        self,
    ):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                self.bootstrap,
                "read_mineru_capacity_file",
                wraps=self.bootstrap.read_mineru_capacity_file,
            ) as reads,
        ):
            original = self.bootstrap.get_process_capacity()
            for key in self.environment:
                for mode in ("missing", "changed"):
                    with self.subTest(key=key, mode=mode), patch.dict(os.environ):
                        if mode == "missing":
                            del os.environ[key]
                        else:
                            os.environ[key] += "-changed"
                        with self.assertRaises(RuntimeError):
                            self.bootstrap.get_process_capacity()
                    self.assertIs(self.bootstrap.get_process_capacity(), original)
            os.environ["UNRELATED_CALLER_VALUE"] = "allowed"
            self.assertIs(self.bootstrap.get_process_capacity(), original)
            self.assertEqual(reads.call_count, 1)

    def test_first_load_rejects_anchor_change_during_actual_file_read(self):
        alternate = self.root / "other-capacity.json"
        alternate.write_bytes(CAPACITY_BYTES)
        alternate.chmod(0o600)
        original = self.bootstrap.read_mineru_capacity_file

        def read_then_change_anchor(*args, **kwargs):
            raw = original(*args, **kwargs)
            os.environ[PATH_KEY] = str(alternate)
            return raw

        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                self.bootstrap,
                "read_mineru_capacity_file",
                side_effect=read_then_change_anchor,
            ) as reads,
        ):
            with self.assertRaises(RuntimeError):
                self.bootstrap.get_process_capacity()
            self.assertEqual(reads.call_count, 1)

    def test_legacy_none_is_cached_without_file_io_and_cannot_later_gain_anchors(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                self.bootstrap,
                "read_mineru_capacity_file",
                side_effect=AssertionError("legacy path must not read"),
            ),
        ):
            self.assertIsNone(self.bootstrap.get_process_capacity())
            os.environ["OMP_NUM_THREADS"] = "legacy caller value"
            self.assertIsNone(self.bootstrap.get_process_capacity())
            os.environ[PATH_KEY] = str(self.file)
            os.environ[SHA_KEY] = self.config.sha256
            with self.assertRaises(RuntimeError):
                self.bootstrap.get_process_capacity()
            del os.environ[PATH_KEY]
            del os.environ[SHA_KEY]
            self.assertIsNone(self.bootstrap.get_process_capacity())

    def test_real_fork_refuses_inherited_owner_before_entering_inherited_locked_mutex(
        self,
    ):
        with patch.dict(os.environ, self.environment, clear=True):
            original = self.bootstrap.get_process_capacity()
            read_fd, write_fd = os.pipe()
            self.bootstrap._PROCESS_LOCK.acquire()
            pid = None
            reaped = False
            try:
                pid = os.fork()
                if pid == 0:
                    os.close(read_fd)
                    try:
                        self.bootstrap.get_process_capacity()
                    except RuntimeError as error:
                        result = (
                            b"rejected"
                            if "inherited process owner" in str(error)
                            else b"wrong-error"
                        )
                    except BaseException:
                        result = b"unexpected-error"
                    else:
                        result = b"accepted-inherited-owner"
                    os.write(write_fd, result)
                    os.close(write_fd)
                    os._exit(0)
                os.close(write_fd)
                write_fd = None
                ready, _, _ = select.select([read_fd], [], [], 2)
                self.assertTrue(ready, "fork child blocked on inherited process mutex")
                self.assertEqual(os.read(read_fd, 128), b"rejected")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    found, status = os.waitpid(pid, os.WNOHANG)
                    if found:
                        reaped = True
                        self.assertTrue(os.WIFEXITED(status))
                        self.assertEqual(os.WEXITSTATUS(status), 0)
                        break
                    time.sleep(0.005)
                self.assertTrue(reaped, "owned fork child did not exit")
            finally:
                self.bootstrap._PROCESS_LOCK.release()
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
                if pid not in (None, 0) and not reaped:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            self.assertIs(self.bootstrap.get_process_capacity(), original)

    def test_unloaded_frameworks_remain_explicitly_unknown_without_importing_or_creating_pool(
        self,
    ):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "torch" or name.startswith(("torch.", "mineru.")):
                raise AssertionError("observation must not import serving frameworks")
            return original_import(name, *args, **kwargs)

        with (
            patch.dict(
                sys.modules, {"torch": None, "mineru.utils.pdf_image_tools": None}
            ),
            patch.dict(
                os.environ,
                {
                    "OMP_NUM_THREADS": "4",
                    "MKL_NUM_THREADS": "2",
                    "OPENBLAS_NUM_THREADS": "1",
                    "MINERU_PDF_RENDER_THREADS": "3",
                },
            ),
            patch.object(builtins, "__import__", side_effect=guarded_import),
        ):
            result = self.observation._framework_limits()
        self.assertEqual(
            result,
            {
                "torch_intraop_threads": unavailable("serving_getter_not_loaded"),
                "pdf_render_pool_max_workers": unavailable(
                    "serving_pool_not_initialized"
                ),
                "mkl_threads": unavailable("no_serving_getter"),
                "openblas_threads": unavailable("no_serving_getter"),
            },
        )

    def test_loaded_getter_and_existing_pool_supply_observations_independent_of_requested_env(
        self,
    ):
        torch = ModuleType("torch")
        torch.get_num_threads = Mock(return_value=6)
        render = ModuleType("mineru.utils.pdf_image_tools")
        render._pdf_render_executor_lock = threading.Lock()
        render._pdf_render_executor = SimpleNamespace(_max_workers=5)
        render._get_pdf_render_executor = Mock(
            side_effect=AssertionError("cannot create pool")
        )
        render._create_pdf_render_executor = Mock(
            side_effect=AssertionError("cannot construct pool")
        )
        with (
            patch.dict(
                sys.modules, {"torch": torch, "mineru.utils.pdf_image_tools": render}
            ),
            patch.dict(os.environ, ENVIRONMENT),
        ):
            result = self.observation._framework_limits()
            self.assertEqual(
                result,
                {
                    "torch_intraop_threads": {
                        "state": "available",
                        "value": 6,
                        "reason": None,
                    },
                    "pdf_render_pool_max_workers": {
                        "state": "available",
                        "value": 5,
                        "reason": None,
                    },
                    "mkl_threads": unavailable("no_serving_getter"),
                    "openblas_threads": unavailable("no_serving_getter"),
                },
            )
            torch.get_num_threads.assert_called_once_with()
            render._get_pdf_render_executor.assert_not_called()
            render._create_pdf_render_executor.assert_not_called()
            self.assertTrue(render._pdf_render_executor_lock.acquire(blocking=False))
            render._pdf_render_executor_lock.release()

    def test_busy_pool_is_unknown_and_invalid_or_failing_getters_stay_visible_with_lock_released(
        self,
    ):
        lock = threading.Lock()
        render = SimpleNamespace(
            _pdf_render_executor_lock=lock,
            _pdf_render_executor=SimpleNamespace(_max_workers=3),
        )
        torch = SimpleNamespace(get_num_threads=Mock(return_value=4))
        with patch.dict(
            sys.modules, {"torch": torch, "mineru.utils.pdf_image_tools": render}
        ):
            lock.acquire()
            result, errors = [], []

            def observe():
                try:
                    result.append(self.observation._framework_limits())
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=observe)
            try:
                thread.start()
                thread.join(1)
                finished_while_busy = not thread.is_alive()
            finally:
                lock.release()
                thread.join(2)
            self.assertTrue(
                finished_while_busy, "observation waited for a busy pool lock"
            )
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                result[0]["pdf_render_pool_max_workers"],
                unavailable("serving_pool_lock_busy"),
            )
            for bad in (True, 0, 3.0):
                render._pdf_render_executor._max_workers = bad
                with self.subTest(pool=bad), self.assertRaises(RuntimeError):
                    self.observation._framework_limits()
                self.assertTrue(lock.acquire(blocking=False))
                lock.release()
            render._pdf_render_executor._max_workers = 3
            marker = OSError("original getter failure")
            torch.get_num_threads.side_effect = marker
            with self.assertRaises(OSError) as caught:
                self.observation._framework_limits()
            self.assertIs(caught.exception, marker)
