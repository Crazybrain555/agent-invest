"""Independent acceptance tests for the MinerU API static-prefix GC lifecycle.

Contract: in the managed Linux CPython 3.12 API process, build the exact Hybrid
static models and freeze the collector's tracked heap once, before a serving
loop, task manager, durable recovery or request exists; keep automatic GC and
its thresholds; unfreeze only after the owned runtime has provably quiesced;
keep every first error visible.

The cases drive the real helper module and the four generated ``fast_api``
lifecycle functions with fake MinerU, torch, capacity and uvicorn owners. Real
collector behaviour runs in child interpreters so this process's heap is never
frozen. No GPU, database, model weight, network or AgentSSD path is used; none
of this qualifies native pause length, CUDA initialisation or long-run
retention on the production CPython 3.12 host.
"""

from __future__ import annotations

import ast
import asyncio
from collections import namedtuple
from collections.abc import Callable
from contextlib import redirect_stderr
import gc
import importlib.util
import io
import itertools
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch
import weakref

from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import (
    TARGET_PREIMAGE_SHA256,
    patch_source,
)
from tests.unit.test_mineru_heap_trim_compat import _named_function, _pinned_preimage


_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL_PATH = (
    _SERVICE_ROOT / "scripts" / "windows" / "mineru_heap_trim_compat" / "agent_task_protocol_v2.py"
)
_PROTOCOL_MODULE = "mineru.cli.agent_task_protocol_v2"
_CAPACITY_ANCHORS = {
    "MINERU_CAPACITY_CONFIG_PATH": "/usr/local/etc/mineru/capacity.json",
    "MINERU_CAPACITY_CONFIG_SHA256": "sha256:" + "4" * 64,
}
_THRESHOLDS = (700, 10, 10)
_MARKER_PREFIX = "MINERU_GC_LIFECYCLE "
_MARKER_REQUIRED = {"policy", "pid", "freeze_count", "automatic_gc", "thresholds"}
_MARKER_ALLOWED = _MARKER_REQUIRED | {"phase"}
_POLICY_CHANGES = {"gc.enable", "gc.disable", "gc.set_threshold", "gc.set_debug"}
_LIFECYCLE_FUNCTIONS = (
    "main", "startup_app_state", "shutdown_app_state", "shutdown_runtime_resources",
)
_SERVING_OWNERS = (
    "uvicorn.Config", "uvicorn.Server", "uvicorn.run", "stdin.watcher", "server.run",
    "manager.construct", "manager.recover_and_dispatch",
)
_CHILD_BOOTSTRAP = (
    "import sys; root = sys.argv[1]; sys.path[:0] = [root, root + '/src']; "
    "from tests.unit.test_mineru_api_gc_lifecycle import _child_main; "
    "_child_main(sys.argv[2])"
)
_VersionInfo = namedtuple("_VersionInfo", "major minor micro releaselevel serial")
_copies = itertools.count()

Event = tuple[Any, ...]


def _install_modules(
    modules: dict[str, ModuleType],
    cleanup: Callable[[Callable[[], None]], None] | None,
) -> None:
    """Bind only the named modules; restore exactly those names on cleanup."""
    absent = object()
    previous = {name: sys.modules.get(name, absent) for name in modules}
    sys.modules.update(modules)
    if cleanup is None:
        return

    def restore() -> None:
        for name, module in previous.items():
            if module is absent:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module  # type: ignore[assignment]

    cleanup(restore)


def _with_packages(modules: dict[str, ModuleType]) -> dict[str, ModuleType]:
    """Add the parent packages, so ``from package import module`` resolves as installed."""
    chain = dict(modules)
    for name in modules:
        parts = name.split(".")
        for depth in range(1, len(parts)):
            parent = ".".join(parts[:depth])
            if parent not in chain:
                package = ModuleType(parent)
                package.__path__ = []
                chain[parent] = package
    for name, module in chain.items():
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(chain[parent], child, module)
    return chain


def _fresh_protocol(cleanup: Callable[[Callable[[], None]], None] | None = None) -> ModuleType:
    """A private helper copy, so each API process starts without a GC epoch."""
    name = f"_r27_api_gc_protocol_{next(_copies)}"
    spec = importlib.util.spec_from_file_location(name, _PROTOCOL_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("the MinerU task protocol helper is not loadable")
    module = importlib.util.module_from_spec(spec)
    _install_modules({name: module}, cleanup)
    spec.loader.exec_module(module)
    return module


def _generated_source(relative_path: str) -> str:
    return patch_source(
        relative_path, _pinned_preimage(relative_path, TARGET_PREIMAGE_SHA256[relative_path])
    )


class _Generated:
    """The generated ``fast_api.py`` lifecycle functions, compiled once without decorators."""

    _cache: tuple[Any, tuple[tuple[str, str], ...]] | None = None

    @classmethod
    def load(cls) -> tuple[Any, tuple[tuple[str, str], ...]]:
        if cls._cache is None:
            tree = ast.parse(_generated_source("mineru/cli/fast_api.py"))
            imports = tuple(
                (alias.asname or alias.name, alias.name)
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == _PROTOCOL_MODULE
                for alias in node.names
            )
            functions = []
            for name in _LIFECYCLE_FUNCTIONS:
                node = _named_function(tree, name)
                node.decorator_list = []
                functions.append(node)
            code = compile(
                ast.Module(body=functions, type_ignores=[]), "generated-fast-api-lifecycle", "exec"
            )
            cls._cache = (code, imports)
        return cls._cache


class _Interpreter:
    """``sys`` as the helper sees it; only the interpreter identity is replaced."""

    def __init__(
        self,
        platform: str = "linux",
        version: tuple[int, int, int] = (3, 12, 13),
        implementation: str = "cpython",
    ) -> None:
        self.platform = platform
        self.version_info = _VersionInfo(*version, "final", 0)
        self.implementation = SimpleNamespace(name=implementation, version=self.version_info)

    def __getattr__(self, name: str) -> Any:
        return getattr(sys, name)


class _Collector:
    """The ``gc`` surface of the lifecycle, recorded instead of freezing this process.

    Counts follow CPython 3.12.13: a fresh API process already reports immortal
    containers in the permanent generation, ``unfreeze`` returns everything to
    the oldest generation and the next full collection moves the immortals
    back. A freeze moves a fixed population, so a freeze that happened stays
    observable when a later step fails.
    """

    DEBUG_SAVEALL = gc.DEBUG_SAVEALL
    # The count read on production 3.12.13; any non-zero value exercises the same
    # rule, and nothing asserts it of a real interpreter.
    IMMORTALS = 375
    POPULATION = 4096
    SEALED = IMMORTALS + POPULATION

    def __init__(self, log: list[Event]) -> None:
        self.log = log
        self.enabled = True
        self.thresholds: tuple[int, ...] = _THRESHOLDS
        self.debug = 0
        self.garbage: list[object] = []
        self.callbacks: list[object] = []
        self.frozen = self.IMMORTALS
        self._immortals_frozen = True
        self.failures: dict[str, BaseException] = {}
        self.after: dict[str, Callable[[], None]] = {}

    def _finish(self, name: str) -> None:
        hook = self.after.get(name)
        if hook is not None:
            hook()
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def isenabled(self) -> bool:
        return self.enabled

    def get_threshold(self) -> tuple[int, ...]:
        return self.thresholds

    def get_debug(self) -> int:
        return self.debug

    def get_freeze_count(self) -> int:
        return self.frozen

    def get_count(self) -> tuple[int, int, int]:
        return (0, 0, 0)

    def collect(self, generation: int = 2) -> int:
        self.log.append(("gc.collect", generation))
        if generation == 2 and not self._immortals_frozen:
            self.frozen += self.IMMORTALS
            self._immortals_frozen = True
        self._finish("collect")
        return 0

    def freeze(self) -> None:
        self.log.append(("gc.freeze",))
        self.frozen += self.POPULATION
        self._finish("freeze")

    def unfreeze(self) -> None:
        self.log.append(("gc.unfreeze",))
        self._finish("unfreeze")
        self.frozen = 0
        self._immortals_frozen = False

    def enable(self) -> None:
        self.log.append(("gc.enable",))
        self.enabled = True

    def disable(self) -> None:
        self.log.append(("gc.disable",))
        self.enabled = False

    def set_threshold(self, *thresholds: int) -> None:
        self.log.append(("gc.set_threshold", thresholds))
        self.thresholds = thresholds

    def set_debug(self, flags: int) -> None:
        self.log.append(("gc.set_debug", flags))
        self.debug = flags


class _StaticModel:
    """A long-lived model root; a ``cyclic`` one needs the collector to be reclaimed.

    ``cache`` is a list because CPython always tracks lists, while a dict of
    atomic values stays untracked and so never enters the frozen generation.
    """

    def __init__(self, role: str, key: object, cyclic: bool) -> None:
        self.role = role
        self.key = key
        self.cache: list[object] = [role]
        if cyclic:
            self.forward = self._forward

    def _forward(self) -> str:
        return self.role


class _ModelRuntime:
    """MinerU 3.4.4 model, device, torch and capacity owners with the installed key rules.

    ``HybridModelSingleton.get_model(lang=None, formula_enable=None)`` keys on
    ``(lang, formula_enable)`` and builds OCR, layout and (with formulas) MFR
    through the shared Atom singleton, as the installed ``model_init.py`` does.
    Every model logs its release, so the moment its last root drops is visible.
    """

    def __init__(self, log: list[Event], *, device: str = "cuda", cyclic: bool = False) -> None:
        self.log = log
        self.device = device
        self.cyclic = cyclic
        self.capacity: object = SimpleNamespace(api_process_limit=1, api_event_loop_limit=1)
        self.construct_failure: BaseException | None = None
        self.sync_failure: BaseException | None = None
        self.on_construct: Callable[[], None] | None = None
        self.static: list[weakref.ref[_StaticModel]] = []
        runtime = self
        lock = threading.RLock()

        class AtomModelSingleton:
            _instance = None
            _models: dict[object, _StaticModel] = {}
            _lock = lock

            def __new__(cls, *args: object, **kwargs: object) -> AtomModelSingleton:
                with cls._lock:
                    if cls._instance is None:
                        cls._instance = super().__new__(cls)
                return cls._instance

            def get_atom_model(self, atom_model_name: str, **kwargs: object) -> _StaticModel:
                if atom_model_name == "ocr":
                    key: tuple[object, ...] = ("ocr", 0.5, kwargs.get("lang"), 1.5, True)
                else:
                    key = (atom_model_name, kwargs.get("device"))
                with self._lock:
                    if key not in self._models:
                        self._models[key] = runtime._model("atom", key)
                return self._models[key]

        class HybridModelSingleton:
            _instance = None
            _models: dict[object, _StaticModel] = {}
            _lock = lock

            def __new__(cls, *args: object, **kwargs: object) -> HybridModelSingleton:
                with cls._lock:
                    if cls._instance is None:
                        cls._instance = super().__new__(cls)
                return cls._instance

            def get_model(self, lang: object = None, formula_enable: object = None) -> _StaticModel:
                key = (lang, formula_enable)
                with self._lock:
                    if key not in self._models:
                        self._models[key] = runtime._hybrid(key)
                return self._models[key]

        self.atom = AtomModelSingleton
        self.hybrid = HybridModelSingleton
        model_init = ModuleType("mineru.backend.pipeline.model_init")
        model_init.PIPELINE_MODEL_INIT_LOCK = lock  # type: ignore[attr-defined]
        model_init.AtomModelSingleton = AtomModelSingleton  # type: ignore[attr-defined]
        model_init.HybridModelSingleton = HybridModelSingleton  # type: ignore[attr-defined]
        config_reader = ModuleType("mineru.utils.config_reader")
        config_reader.get_device = lambda: runtime.device  # type: ignore[attr-defined]
        torch = ModuleType("torch")
        torch.cuda = SimpleNamespace(  # type: ignore[attr-defined]
            synchronize=self._synchronize,
            is_available=lambda: runtime.device.startswith("cuda"),
        )
        capacity = ModuleType("mineru.cli.agent_capacity_bootstrap")
        capacity.get_process_capacity = self._capacity  # type: ignore[attr-defined]
        self.modules = {
            "mineru.backend.pipeline.model_init": model_init,
            "mineru.utils.config_reader": config_reader,
            "torch": torch,
            "mineru.cli.agent_capacity_bootstrap": capacity,
        }

    def _model(self, role: str, key: object) -> _StaticModel:
        model = _StaticModel(role, key, self.cyclic)
        weakref.finalize(model, self.log.append, ("model.released", role, key))
        self.static.append(weakref.ref(model))
        return model

    def _hybrid(self, key: tuple[object, object]) -> _StaticModel:
        self.log.append(("hybrid.construct", key))
        if self.on_construct is not None:
            self.on_construct()
        if self.construct_failure is not None:
            raise self.construct_failure
        lang, formula_enable = key
        atom = self.atom()
        model = self._model("hybrid", key)
        model.ocr_model = atom.get_atom_model("ocr", lang=lang)  # type: ignore[attr-defined]
        model.layout_model = atom.get_atom_model(  # type: ignore[attr-defined]
            "layout", device=self.device
        )
        if formula_enable:
            model.mfr_model = atom.get_atom_model(  # type: ignore[attr-defined]
                "mfr", device=self.device
            )
        return model

    def _synchronize(self, device: object = None) -> None:
        self.log.append(("torch.cuda.synchronize", device))
        if self.sync_failure is not None:
            raise self.sync_failure

    def _capacity(self) -> object:
        self.log.append(("capacity.load",))
        return self.capacity


# MinerU 3.4.4 ``mineru/utils/pdf_image_tools.py`` final render shutdown, restated
# for the fake owners: the executor root is cleared first, every terminate, join,
# kill and executor failure is swallowed, and liveness is never re-checked.
_RENDER_SHUTDOWN = '''
def _terminate_executor_processes(executor):
    started = [p for p in (getattr(executor, "_processes", None) or {}).values() if p.is_alive()]
    for process in started:
        try:
            process.terminate()
        except Exception:
            pass
    for process in started:
        try:
            process.join(timeout=PDF_RENDER_TERMINATE_GRACE_PERIOD_SECONDS)
        except Exception:
            pass
    for process in started:
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
    for process in started:
        if process.is_alive():
            try:
                process.join(timeout=PDF_RENDER_KILL_JOIN_TIMEOUT_SECONDS)
            except Exception:
                pass


def _recycle_pdf_render_executor(executor, *, terminate_processes):
    global _pdf_render_executor
    if executor is None:
        return
    with _pdf_render_executor_lock:
        if _pdf_render_executor is executor:
            _pdf_render_executor = None
    if terminate_processes:
        try:
            _terminate_executor_processes(executor)
        except Exception as exc:
            logger.warning(f"Failed to terminate PDF render executor processes: {exc}")
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        logger.warning(f"Failed to shutdown PDF render executor: {exc}")


def shutdown_pdf_render_executor():
    global _pdf_render_executor
    _record_close()
    with _pdf_render_executor_lock:
        executor = _pdf_render_executor
        _pdf_render_executor = None
    if executor is not None:
        _recycle_pdf_render_executor(executor, terminate_processes=True)
'''


_RENDER_GRACE_SECONDS = 0.05
_RENDER_KILL_JOIN_SECONDS = 0.02


class _RenderWorker:
    """A spawned render process.

    ``exits_on`` is ``"terminate"`` or ``"kill"``; ``"never"`` accepts every
    signal silently and stays alive (uninterruptible); ``"unreadable"`` stays
    alive and cannot report liveness; ``None`` refuses every signal and join
    with an error.
    """

    def __init__(self, exits_on: str | None) -> None:
        self.exits_on = exits_on
        self.alive = True
        self.exitcode: int | None = None
        self.signals: list[str] = []
        self.join_timeouts: list[float | None] = []
        self.raised: list[BaseException] = []

    def is_alive(self) -> bool:
        if self.exits_on == "unreadable":
            raise ValueError("process object is closed")
        return self.alive

    def _refuse(self, operation: str) -> None:
        self.raised.append(OSError(f"render worker refused {operation}"))
        raise self.raised[-1]

    def _signal(self, name: str, exitcode: int) -> None:
        self.signals.append(name)
        if self.exits_on is None:
            self._refuse(name)
        if self.exits_on == name:
            self.alive = False
            self.exitcode = exitcode

    def terminate(self) -> None:
        self._signal("terminate", -15)

    def kill(self) -> None:
        self._signal("kill", -9)

    def join(self, timeout: float | None = None) -> None:
        self.signals.append("join")
        self.join_timeouts.append(timeout)
        if self.exits_on is None:
            self._refuse("join")


class _ManagerThread:
    """The pool's executor manager thread: settles on join, or is still cleaning up."""

    def __init__(self, settles: bool = True, join_error: BaseException | None = None) -> None:
        self.settles = settles
        self.join_error = join_error
        self.alive = True
        self.join_timeouts: list[float | None] = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)
        if self.join_error is not None:
            raise self.join_error
        if self.settles:
            self.alive = False


class _RenderPool:
    """``ProcessPoolExecutor`` as MinerU's shutdown path touches it.

    Like CPython's, ``shutdown(wait=False)`` drops the pool's references to its
    workers and to its manager thread without waiting for either.
    """

    def __init__(
        self, log: list[Event], *workers: _RenderWorker, manager: _ManagerThread | None = None,
    ) -> None:
        self.log = log
        self._processes: dict[int, _RenderWorker] | None = dict(enumerate(workers, 1))
        self._executor_manager_thread = manager
        self._max_workers = max(1, len(workers))
        self.shutdown_error: BaseException | None = None

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.log.append(("render.pool.shutdown", wait, cancel_futures))
        if self.shutdown_error is not None:
            raise self.shutdown_error
        if any(worker.exits_on is None for worker in (self._processes or {}).values()):
            raise OSError("render executor wakeup pipe failed")
        self._executor_manager_thread = None
        self._processes = None


def _render_tools(log: list[Event], record_close: Callable[[], None]) -> ModuleType:
    module = ModuleType("mineru.utils.pdf_image_tools")
    module.__dict__.update({
        "time": time,
        "_pdf_render_executor": None,
        "_pdf_render_executor_lock": threading.Lock(),
        "PDF_RENDER_TERMINATE_GRACE_PERIOD_SECONDS": _RENDER_GRACE_SECONDS,
        "PDF_RENDER_KILL_JOIN_TIMEOUT_SECONDS": _RENDER_KILL_JOIN_SECONDS,
        "logger": SimpleNamespace(
            warning=lambda message: log.append(("render.warning", message)),
            debug=lambda message: None,
        ),
        "_record_close": record_close,
    })
    exec(compile(_RENDER_SHUTDOWN, "mineru-3.4.4-render-shutdown", "exec"), module.__dict__)
    return module


class _LoggedStream(io.StringIO):
    """stderr that also records every written line in the shared event log."""

    def __init__(self, log: list[Event]) -> None:
        super().__init__()
        self._events = log

    def write(self, text: str) -> int:
        written = super().write(text)
        if text.strip():
            self._events.append(("stderr", text.strip()))
        return written


def _app(*, preload: bool = False, manager: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(
        task_manager=manager, service_config={"enable_vlm_preload": preload}, config={},
    ))


def _split_config(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    service = {"enable_vlm_preload": bool(raw.get("enable_vlm_preload", False))}
    return service, {key: value for key, value in raw.items() if key != "enable_vlm_preload"}


async def _settle(awaitable: Any) -> Any:
    return await awaitable


async def _to_thread_owned(function: Callable[..., Any], *args: Any) -> Any:
    """Run a native close on its own thread, bounded so a lock-order deadlock fails the case."""
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future[Any] = loop.create_future()

    def settle(error: BaseException | None, value: Any) -> None:
        if outcome.done():
            return
        if error is None:
            outcome.set_result(value)
        else:
            outcome.set_exception(error)

    def run() -> None:
        try:
            value = function(*args)
        except BaseException as exc:
            loop.call_soon_threadsafe(settle, exc, None)
        else:
            loop.call_soon_threadsafe(settle, None, value)

    threading.Thread(target=run, name="owned-native-close", daemon=True).start()
    return await asyncio.wait_for(outcome, 30)


async def _raise_later(failure: BaseException) -> None:
    await asyncio.sleep(0)
    raise failure


class _Server:
    """uvicorn's ``Server.run`` contract as the CLI sees it.

    Production runs uvicorn 0.47.0, whose ``run`` returns normally after a failed
    lifespan startup (newer releases exit with status 3 instead). A failed
    lifespan shutdown is only logged and ``run`` returns; a captured SIGINT is
    re-raised after the graceful shutdown; a second SIGINT (force exit) skips
    the lifespan shutdown.
    """

    def __init__(self, api: _Api) -> None:
        self.api = api

    def run(self) -> None:
        api = self.api
        api.log.append(("server.run",))
        started = asyncio.run(self._serve())
        api.log.append(("server.returned",))
        if not started:
            if api.startup_failure_exits:
                raise SystemExit(3)
            return
        if api.server_exit in {"signal", "force_exit"}:
            raise api.signal

    async def _serve(self) -> bool:
        api = self.api
        try:
            await api.namespace["startup_app_state"](api.app)
        except Exception as exc:
            api.log.append(("lifespan.startup_failed", exc))
            return False
        api.log.append(("server.serving",))
        await asyncio.sleep(0)
        if api.server_exit == "force_exit":
            return True
        try:
            await api.namespace["shutdown_app_state"](api.app)
        except Exception as exc:
            api.log.append(("lifespan.shutdown_failed", exc))
        return True


class _Uvicorn:
    def __init__(self, api: _Api) -> None:
        self.api = api

    def Config(self, app: object, **options: object) -> SimpleNamespace:
        self.api.log.append(("uvicorn.Config",))
        return SimpleNamespace(app=app, options=options)

    def Server(self, config: object) -> _Server:
        self.api.log.append(("uvicorn.Server",))
        if self.api.server_failure is not None:
            raise self.api.server_failure
        return _Server(self.api)

    def run(self, target: object, **options: object) -> None:
        self.api.log.append(("uvicorn.run", options.get("reload")))


class _Api:
    """One API process: a fresh helper copy, a recorded collector and fake owners."""

    def __init__(
        self,
        test: unittest.TestCase,
        *,
        managed: bool = True,
        device: str = "cuda",
        interpreter: _Interpreter | None = None,
        anchors: dict[str, str] | None = None,
    ) -> None:
        self.log: list[Event] = []
        self.protocol = _fresh_protocol(test.addCleanup)
        self.collector = _Collector(self.log)
        test.enterContext(patch.object(self.protocol, "gc", self.collector))
        test.enterContext(patch.object(self.protocol, "sys", interpreter or _Interpreter()))
        self.runtime = _ModelRuntime(self.log, device=device)
        self.render = _render_tools(
            self.log, lambda: self.log.append(("render.upstream_shutdown",))
        )
        self.pool = _RenderPool(self.log, _RenderWorker("terminate"), manager=_ManagerThread())
        self.render._pdf_render_executor = self.pool
        _install_modules(
            _with_packages({**self.runtime.modules, "mineru.utils.pdf_image_tools": self.render}),
            test.addCleanup,
        )
        test.enterContext(patch.dict(os.environ))
        for name in _CAPACITY_ANCHORS:
            os.environ.pop(name, None)
        os.environ.update(anchors if anchors is not None else _CAPACITY_ANCHORS if managed else {})
        self.stderr = _LoggedStream(self.log)
        test.enterContext(redirect_stderr(self.stderr))
        self.app = _app()
        self.namespace: dict[str, Any] = {}
        self.server_exit = "graceful"
        self.startup_failure_exits = False
        self.signal = KeyboardInterrupt()
        self.server_failure: BaseException | None = None
        self.recovery_failure: BaseException | None = None
        self.drain_failure: BaseException | None = None
        self.task_failure: BaseException | None = None
        self.io_failure: BaseException | None = None
        self.models_close_failure: BaseException | None = None

    def generated(self) -> dict[str, Any]:
        code, imports = _Generated.load()
        namespace: dict[str, Any] = {
            local: getattr(self.protocol, name)
            for local, name in imports
            if hasattr(self.protocol, name)
        }
        namespace.update({
            "asyncio": asyncio, "os": os, "sys": sys, "FastAPI": object, "app": self.app,
            "AsyncTaskManager": self._manager_class(),
            "maybe_preload_vlm_model": self._preload,
            "_settle_service_operation": _settle,
            "to_thread_owned": _to_thread_owned,
            "shutdown_cached_models": self._close_models,
            "shutdown_pdf_render_executor": self.render.shutdown_pdf_render_executor,
            "logger": SimpleNamespace(
                warning=lambda message: self.log.append(("logger.warning", message)),
                info=lambda message: None,
            ),
            "arg_parse": lambda ctx: {},
            "split_service_and_model_config": _split_config,
            "is_public_bind_host": lambda host: False,
            "configure_public_http_client_policy": lambda app, **options: None,
            "MINERU_API_PUBLIC_BIND_EXPOSED_ENV": "MINERU_API_PUBLIC_BIND_EXPOSED",
            "MINERU_API_ALLOW_PUBLIC_HTTP_CLIENT_ENV": "MINERU_API_ALLOW_PUBLIC_HTTP_CLIENT",
            "warn_if_public_http_client_policy": lambda host, allow: None,
            "env_flag_enabled": lambda name, default=False: default,
            "uvicorn": _Uvicorn(self),
            "install_stdin_shutdown_watcher": lambda server: self.log.append(("stdin.watcher",)),
            "print": lambda *args, **kwargs: None,
        })
        exec(code, namespace)
        self.namespace = namespace
        return namespace

    def run_main(self, *, reload: bool = False, enable_vlm_preload: bool = False) -> None:
        namespace = self.namespace or self.generated()
        namespace["main"](None, "127.0.0.1", 8000, reload, False, enable_vlm_preload)

    def _preload(self, enabled: bool, model_kwargs: object = None) -> None:
        self.log.append(("vlm.preload", enabled))

    def _close_models(self) -> None:
        self.log.append(("runtime.close_models",))
        if self.models_close_failure is not None:
            raise self.models_close_failure

    def _manager_class(self) -> type:
        api = self

        class AsyncTaskManager:
            def __init__(self, app: object) -> None:
                api.log.append(("manager.construct", api.collector.frozen))
                self.dispatcher_task: asyncio.Task[Any] | None = None
                self.cleanup_task: asyncio.Task[Any] | None = None
                self.active_tasks: set[asyncio.Task[Any]] = set()
                self.serving_loop_probe = SimpleNamespace(
                    close=lambda: api.log.append(("probe.close",))
                )
                self.service_io = SimpleNamespace(close=self._close_io)

            async def start(self) -> None:
                api.log.append(("manager.recover_and_dispatch",))
                if api.recovery_failure is not None:
                    raise api.recovery_failure
                self.dispatcher_task = asyncio.create_task(asyncio.Event().wait())
                if api.task_failure is not None:
                    self.active_tasks.add(asyncio.create_task(_raise_later(api.task_failure)))

            async def shutdown(self) -> None:
                api.log.append(("manager.shutdown",))
                if api.drain_failure is not None:
                    raise api.drain_failure

            async def _close_io(self) -> None:
                api.log.append(("service_io.close",))
                if api.io_failure is not None:
                    raise api.io_failure

        return AsyncTaskManager


def _kinds(log: list[Event]) -> list[Any]:
    return [event[0] for event in log]


def _first(log: list[Event], kind: str) -> int:
    for index, event in enumerate(log):
        if event[0] == kind:
            return index
    raise AssertionError(f"{kind} never happened: {_kinds(log)}")


def _all(log: list[Event], kind: str) -> list[int]:
    return [index for index, event in enumerate(log) if event[0] == kind]


def _last(log: list[Event], event: Event) -> int:
    matches = [index for index, value in enumerate(log) if value == event]
    if not matches:
        raise AssertionError(f"{event} never happened: {_kinds(log)}")
    return matches[-1]


def _collector_events(log: list[Event]) -> list[Event]:
    return [event for event in log if str(event[0]).startswith("gc.")]


def _seal_positions(log: list[Event]) -> list[int]:
    """Indices of collect(0), collect(1), collect(2) and freeze, in that order."""
    positions: list[int] = []
    start = 0
    for step in (("gc.collect", 0), ("gc.collect", 1), ("gc.collect", 2), ("gc.freeze",)):
        index = next((i for i in range(start, len(log)) if log[i] == step), None)
        if index is None:
            raise AssertionError(f"seal step {step} missing after {start}: {_kinds(log)}")
        positions.append(index)
        start = index + 1
    return positions


def _carries(primary: BaseException, secondary: BaseException) -> bool:
    """Whether ``secondary`` stays visible on ``primary`` (note, cause or context)."""
    notes = " ".join(getattr(primary, "__notes__", ()))
    return (
        repr(secondary) in notes
        or primary.__cause__ is secondary
        or primary.__context__ is secondary
    )


def _carries_diagnostic(primary: BaseException) -> bool:
    return bool(getattr(primary, "__notes__", None)) or primary.__cause__ is not None


def _facts(log: list[Event]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, json.loads(event[1][len(_MARKER_PREFIX):]))
        for index, event in enumerate(log)
        if event[0] == "stderr" and event[1].startswith(_MARKER_PREFIX)
    ]


def _window_path_hybrid_keys() -> set[tuple[None, bool]]:
    """Hybrid singleton keys the pinned 3.4.4 window path can request."""
    tree = ast.parse(_generated_source("mineru/backend/hybrid/hybrid_analyze.py"))
    window = _named_function(tree, "_predict_layout_for_window")
    calls = [
        node for node in ast.walk(window)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_model"
    ]
    if len(calls) != 1:
        raise AssertionError("the window path no longer has one Hybrid model lookup")
    (call,) = calls
    if call.args or [keyword.arg for keyword in call.keywords] != ["formula_enable"]:
        raise AssertionError("the window path Hybrid key shape changed")
    if not isinstance(call.keywords[0].value, ast.BoolOp):
        raise AssertionError("the window path formula flag is no longer a boolean expression")
    # get_model(lang=None, formula_enable=None) keys on (lang, formula_enable).
    return {(None, False), (None, True)}


def _in_thread(function: Callable[[], object]) -> BaseException | None:
    outcome: list[BaseException | None] = []

    def target() -> None:
        try:
            function()
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=target, name="lifespan-elsewhere")
    worker.start()
    worker.join(30)
    if not outcome:
        raise AssertionError("the lifecycle call did not return on its thread")
    return outcome[0]


class GeneratedApiLifecycleTests(unittest.TestCase):
    """The generated CLI, lifespan and runtime close, wired to the real helper."""

    def _assert_prefix_kept(self, api: _Api) -> None:
        kinds = _kinds(api.log)
        self.assertNotIn("gc.unfreeze", kinds)
        self.assertNotIn("model.released", kinds)
        self.assertEqual(api.collector.frozen, _Collector.SEALED)
        self.assertEqual(api.log.count(("gc.collect", 2)), 1)

    def test_static_prefix_is_sealed_before_serving_and_released_after_quiescence(self) -> None:
        # uvicorn re-raises a captured SIGINT, and exits 1 on a bind failure, only
        # after the lifespan shutdown has completed.
        for exit_mode, after_shutdown in (("graceful", None),
                                          ("captured SIGINT", KeyboardInterrupt()),
                                          ("bind failure", SystemExit(1))):
            with self.subTest(exit=exit_mode):
                api = _Api(self)
                if after_shutdown is None:
                    api.run_main()
                else:
                    api.server_exit = "signal"
                    api.signal = after_shutdown
                    with self.assertRaises(BaseException) as raised:
                        api.run_main()
                    self.assertIs(raised.exception, after_shutdown)
                log = api.log

                self.assertCountEqual(
                    [event[1] for event in log if event[0] == "hybrid.construct"],
                    [(None, False), (None, True)],
                )
                seal = _seal_positions(log)
                self.assertEqual(_all(log, "gc.freeze"), [seal[-1]])
                built = _all(log, "hybrid.construct") + _all(log, "torch.cuda.synchronize")
                self.assertLess(max(built), seal[0])
                for owner in ("uvicorn.Config", "uvicorn.Server", "stdin.watcher", "server.run",
                              "manager.construct", "manager.recover_and_dispatch"):
                    self.assertGreater(_first(log, owner), seal[-1], owner)
                self.assertEqual(log[_first(log, "manager.construct")][1], _Collector.SEALED)

                release = _first(log, "gc.unfreeze")
                self.assertEqual(_collector_events(log[seal[-1] + 1:release]), [])
                self.assertFalse(api.pool._processes)
                for done in ("manager.shutdown", "service_io.close", "runtime.close_models",
                             "render.pool.shutdown", "server.returned"):
                    self.assertLess(_first(log, done), release, done)
                released = _all(log, "model.released")
                self.assertEqual(len(released), len(api.runtime.static))
                self.assertLess(release, min(released))
                self.assertLess(max(released), _last(log, ("gc.collect", 2)))
                # CPython 3.12 immortals return to the permanent generation; the seal does not.
                self.assertEqual(api.collector.frozen, _Collector.IMMORTALS)
                self.assertTrue(api.collector.enabled)
                self.assertEqual(api.collector.thresholds, _THRESHOLDS)
                self.assertFalse([event for event in log if event[0] in _POLICY_CHANGES])

                facts = _facts(log)
                startup = [(index, fact) for index, fact in facts if index < release]
                self.assertEqual(len(startup), 1, facts)
                index, fact = startup[0]
                self.assertLess(seal[-1], index)
                self.assertLess(index, _first(log, "server.run"))
                self.assertLessEqual(_MARKER_REQUIRED, set(fact))
                self.assertLessEqual(set(fact), _MARKER_ALLOWED)
                self.assertIsInstance(fact["policy"], str)
                self.assertEqual(fact["pid"], os.getpid())
                self.assertEqual(fact["freeze_count"], _Collector.SEALED)
                self.assertIs(fact["automatic_gc"], True)
                self.assertEqual(fact["thresholds"], list(_THRESHOLDS))

    def test_failure_before_serving_never_reaches_the_lifespan_and_keeps_the_first_error(
        self,
    ) -> None:
        def weights(api: _Api) -> object:
            api.runtime.construct_failure = OSError("hybrid weights unavailable")
            return api.runtime.construct_failure

        def device(api: _Api) -> object:
            api.runtime.sync_failure = RuntimeError("CUDA launch failure during warmup")
            return api.runtime.sync_failure

        def drift(api: _Api) -> object:
            api.runtime.on_construct = lambda: setattr(api.collector, "thresholds", (1, 1, 1))
            return RuntimeError

        def unwritable(api: _Api) -> object:
            api.stderr.close()
            return ValueError

        def server(api: _Api) -> object:
            api.server_failure = RuntimeError("uvicorn server construction failed")
            return api.server_failure

        for name, arrange, sealed in (
            ("model weights", weights, False), ("device sync", device, False),
            ("policy drift", drift, False), ("startup fact", unwritable, False),
            ("server construction", server, True),
        ):
            with self.subTest(failure=name):
                api = _Api(self)
                expected = arrange(api)
                with self.assertRaises(BaseException) as raised:
                    api.run_main()
                if isinstance(expected, BaseException):
                    self.assertIs(raised.exception, expected)
                else:
                    self.assertIsInstance(raised.exception, expected)  # type: ignore[arg-type]
                kinds = _kinds(api.log)
                for owner in ("manager.construct", "manager.recover_and_dispatch", "server.serving"):
                    self.assertNotIn(owner, kinds)
                self.assertFalse([event for event in api.log if event[0] in _POLICY_CHANGES])
                if not sealed:
                    self.assertNotIn("uvicorn.Server", kinds)
                    self.assertLessEqual(api.collector.frozen, _Collector.IMMORTALS)
                if "gc.unfreeze" in kinds:
                    released = _all(api.log, "model.released")
                    self.assertTrue(released)
                    self.assertLess(_first(api.log, "gc.unfreeze"), min(released))
                # A seal that failed or was released never serves a later lifespan.
                with self.assertRaises(RuntimeError):
                    asyncio.run(api.namespace["startup_app_state"](api.app))
                self.assertNotIn("manager.construct", _kinds(api.log))

    def test_managed_options_that_break_the_seal_are_refused_before_models(self) -> None:
        for name, options in (("reload", {"reload": True}),
                              ("local VLM preload", {"enable_vlm_preload": True})):
            with self.subTest(option=name):
                api = _Api(self)
                with self.assertRaises(RuntimeError):
                    api.run_main(**options)
                kinds = _kinds(api.log)
                for owner in _SERVING_OWNERS + ("hybrid.construct",):
                    self.assertNotIn(owner, kinds)
                self.assertEqual(_collector_events(api.log), [])
        legacy = _Api(self, managed=False)
        legacy.run_main(reload=True)
        self.assertIn(("uvicorn.run", True), legacy.log)
        self.assertNotIn("hybrid.construct", _kinds(legacy.log))
        self.assertEqual(_collector_events(legacy.log), [])

    def test_a_lifespan_without_the_cli_seal_never_recovers_tasks(self) -> None:
        managed = _Api(self)
        namespace = managed.generated()
        with self.assertRaises(RuntimeError):
            asyncio.run(namespace["startup_app_state"](managed.app))
        self.assertNotIn("manager.construct", _kinds(managed.log))
        self.assertEqual(_collector_events(managed.log), [])

        legacy = _Api(self, managed=False)
        namespace = legacy.generated()

        async def lifespan() -> None:
            await namespace["startup_app_state"](legacy.app)
            await namespace["shutdown_app_state"](legacy.app)

        asyncio.run(lifespan())
        kinds = _kinds(legacy.log)
        self.assertLess(kinds.index("manager.construct"), kinds.index("manager.recover_and_dispatch"))
        self.assertIn("render.pool.shutdown", kinds)
        self.assertNotIn("hybrid.construct", kinds)
        self.assertEqual(_collector_events(legacy.log), [])

    def test_a_shutdown_that_cannot_prove_quiescence_fails_visibly_and_keeps_the_seal(
        self,
    ) -> None:
        for name, knob, failure in (
            ("dispatcher drain", "drain_failure", RuntimeError("dispatcher drain failed")),
            ("owned parse task", "task_failure", RuntimeError("owned parse task failed")),
            ("service io", "io_failure", OSError("registry service io close failed")),
            ("cached models", "models_close_failure", RuntimeError("VLM model close failed")),
            ("render executor", "pool_failure", OSError("render executor shutdown failed")),
        ):
            with self.subTest(failure=name):
                api = _Api(self)
                if knob == "pool_failure":
                    api.pool.shutdown_error = failure
                else:
                    setattr(api, knob, failure)
                # uvicorn only logs a failed lifespan shutdown and returns normally.
                with self.assertRaises(BaseException):
                    api.run_main()
                self.assertIn("lifespan.shutdown_failed", _kinds(api.log))
                self._assert_prefix_kept(api)

        forced = _Api(self)
        forced.server_exit = "force_exit"
        with self.assertRaises(KeyboardInterrupt) as interrupted:
            forced.run_main()
        self.assertIs(interrupted.exception, forced.signal)
        self.assertTrue(_carries_diagnostic(interrupted.exception))
        self._assert_prefix_kept(forced)

        for uvicorn, exits in (("0.47 returns", False), ("newer exits 3", True)):
            with self.subTest(recovery_failure=uvicorn):
                recovery = _Api(self)
                recovery.startup_failure_exits = exits
                recovery.recovery_failure = RuntimeError("durable recovery failed")
                with self.assertRaises(BaseException) as failed:
                    recovery.run_main()
                if exits:
                    self.assertIsInstance(failed.exception, SystemExit)
                    self.assertEqual(failed.exception.code, 3)  # type: ignore[attr-defined]
                    self.assertTrue(_carries_diagnostic(failed.exception))
                self.assertIn("lifespan.startup_failed", _kinds(recovery.log))
                self.assertIn("manager.recover_and_dispatch", _kinds(recovery.log))
                self._assert_prefix_kept(recovery)

    def test_final_render_cleanup_must_prove_workers_and_manager_finished(self) -> None:
        # MinerU's own final render shutdown clears its root and swallows every
        # terminate/join/kill/executor failure without re-checking anything, and
        # shutdown(wait=False) drops the manager thread that may still be cleaning
        # up. Any render owner not proven finished must block quiescence.
        def pool(api: _Api, *workers: _RenderWorker, manager: _ManagerThread) -> _RenderPool:
            return _RenderPool(api.log, *workers, manager=manager)

        survivors = (
            ("worker refuses signals", (_RenderWorker("terminate"), _RenderWorker(None)),
             _ManagerThread()),
            ("worker ignores signals", (_RenderWorker("terminate"), _RenderWorker("never")),
             _ManagerThread()),
            ("worker liveness unreadable", (_RenderWorker("unreadable"),), _ManagerThread()),
            ("manager still cleaning up", (_RenderWorker("terminate"),),
             _ManagerThread(settles=False)),
            ("manager join fails", (_RenderWorker("terminate"),),
             _ManagerThread(join_error=RuntimeError("cannot join the manager thread"))),
        )
        for name, workers, manager in survivors:
            with self.subTest(survivor=name):
                api = _Api(self)
                api.render._pdf_render_executor = pool(api, *workers, manager=manager)
                with self.assertRaises(BaseException):
                    api.run_main()
                self.assertTrue(all(worker.signals[0] == "terminate" for worker in workers))
                self.assertTrue(any(worker.alive for worker in workers) or manager.alive)
                # The final stop keeps MinerU's own bounded waits; nothing waits forever.
                joins = [t for worker in workers for t in worker.join_timeouts]
                joins += manager.join_timeouts
                self.assertTrue(joins)
                for timeout in joins:
                    self.assertIsNotNone(timeout)
                    self.assertLessEqual(timeout, _RENDER_GRACE_SECONDS)
                self.assertIn("lifespan.shutdown_failed", _kinds(api.log))
                self._assert_prefix_kept(api)

        for name, workers, manager in (
            ("exit on SIGTERM", (_RenderWorker("terminate"), _RenderWorker("terminate")),
             _ManagerThread()),
            ("exit only on SIGKILL", (_RenderWorker("kill"),), _ManagerThread()),
            ("manager never started", (_RenderWorker("terminate"),), None),
            ("never rendered", None, None),
        ):
            with self.subTest(render=name):
                api = _Api(self)
                api.render._pdf_render_executor = (
                    None if workers is None else pool(api, *workers, manager=manager)
                )
                api.run_main()
                self.assertFalse([worker for worker in workers or () if worker.alive])
                self.assertFalse(manager is not None and manager.alive)
                if workers is not None:
                    self.assertLess(
                        _first(api.log, "render.pool.shutdown"), _first(api.log, "gc.unfreeze")
                    )
                self.assertIsNone(api.render._pdf_render_executor)
                self.assertEqual(api.collector.frozen, _Collector.IMMORTALS)

    def test_runtime_close_attempts_every_native_owner_and_raises_the_first_error(
        self,
    ) -> None:
        for name, models_error, render_error in (
            ("clean", None, None),
            ("models", RuntimeError("VLM model close failed"), None),
            ("render", None, OSError("render executor close failed")),
            ("both", RuntimeError("model close failed first"), OSError("render failed second")),
            ("interrupt", KeyboardInterrupt(), None),
        ):
            with self.subTest(failure=name):
                api = _Api(self)
                api.models_close_failure = models_error
                api.pool.shutdown_error = render_error
                close = api.generated()["shutdown_runtime_resources"]
                expected = models_error or render_error
                if expected is None:
                    close()
                else:
                    with self.assertRaises(BaseException) as raised:
                        close()
                    self.assertIs(raised.exception, expected)
                    if models_error is not None and render_error is not None:
                        self.assertTrue(_carries(models_error, render_error))
                self.assertEqual(
                    [kind for kind in _kinds(api.log)
                     if kind in {"runtime.close_models", "render.pool.shutdown"}],
                    ["runtime.close_models", "render.pool.shutdown"],
                )
                self.assertFalse(api.pool._processes and any(
                    worker.is_alive() for worker in api.pool._processes.values()
                ))

        # Inside the render close, the first failure stays primary and later
        # ones (a manager that cannot be joined) ride on it.
        api = _Api(self)
        refusing = _RenderWorker(None)
        stuck = RuntimeError("cannot join the manager thread")
        api.pool = _RenderPool(api.log, refusing, manager=_ManagerThread(join_error=stuck))
        api.render._pdf_render_executor = api.pool
        with self.assertRaises(BaseException) as raised:
            api.generated()["shutdown_runtime_resources"]()
        self.assertIs(raised.exception, refusing.raised[0])
        self.assertTrue(_carries(raised.exception, stuck))

    def test_generated_api_holds_no_collector_policy_and_import_stays_inert(self) -> None:
        tree = ast.parse(_generated_source("mineru/cli/fast_api.py"))
        self.assertEqual(
            [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "gc"], []
        )
        self.assertEqual([
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
            if alias.name == "gc" or getattr(node, "module", None) == "gc"
        ], [])
        protocol_names = {local for local, _ in _Generated.load()[1]}
        import_time_calls = [
            node.func.id
            for statement in tree.body
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in protocol_names
        ]
        self.assertEqual(import_time_calls, [])

        helper = ast.parse(_PROTOCOL_PATH.read_text(encoding="utf-8"))
        collector = [
            node.attr for node in ast.walk(helper)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "gc"
        ]
        self.assertEqual(collector.count("freeze"), 1)
        self.assertEqual({"enable", "disable", "set_threshold", "set_debug"} & set(collector), set())


class StaticModelKeyTests(unittest.TestCase):
    """The prewarm builds exactly the singleton entries the window path reads."""

    def test_prewarm_builds_exactly_the_hybrid_keys_the_window_path_requests(self) -> None:
        pinned = {
            function.name
            for relative_path in TARGET_PREIMAGE_SHA256
            for function in ast.walk(ast.parse(_generated_source(relative_path)))
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(node, ast.Name) and node.id == "HybridModelSingleton"
                    for node in ast.walk(function))
        }
        self.assertEqual(pinned, {"_predict_layout_for_window"})
        keys = _window_path_hybrid_keys()

        api = _Api(self)
        api.protocol.bootstrap_api_gc(api.app, reload=False)
        self.assertCountEqual(
            [event[1] for event in api.log if event[0] == "hybrid.construct"], sorted(keys)
        )
        sealed = dict(api.runtime.hybrid._models)
        self.assertEqual(set(sealed), keys)
        settled = len(api.log)
        window_singleton = api.runtime.hybrid()
        for inline_formula_enable, ocr_enable in itertools.product((False, True), repeat=2):
            formula_enable = inline_formula_enable and not ocr_enable
            self.assertIs(
                window_singleton.get_model(formula_enable=formula_enable),
                sealed[(None, formula_enable)],
            )
        self.assertNotIn("hybrid.construct", _kinds(api.log[settled:]))

    def test_only_cuda_construction_is_synchronized_before_the_seal(self) -> None:
        for device, synchronized in (("cuda", True), ("cuda:0", True), ("cpu", False),
                                     ("mps", False)):
            with self.subTest(device=device):
                api = _Api(self, device=device)
                api.protocol.bootstrap_api_gc(api.app, reload=False)
                seal = _seal_positions(api.log)
                syncs = _all(api.log, "torch.cuda.synchronize")
                if not synchronized:
                    self.assertEqual(syncs, [])
                    continue
                self.assertEqual(len(syncs), 1)
                self.assertIn(api.log[syncs[0]][1], {device, None})
                self.assertLess(max(_all(api.log, "hybrid.construct")), syncs[0])
                self.assertLess(syncs[0], seal[0])


class ManagedEntryTests(unittest.TestCase):
    """Only the explicit-capacity Linux CPython 3.12 API main process seals."""

    def test_unmanaged_api_is_inert_on_any_interpreter(self) -> None:
        api = _Api(self, managed=False, interpreter=_Interpreter("darwin", (3, 13, 13)))
        api.protocol.bootstrap_api_gc(_app(preload=True, manager=object()), reload=True)
        api.protocol.enter_api_gc_runtime()
        api.protocol.mark_api_gc_quiesced()
        api.protocol.close_api_gc()
        self.assertEqual(api.log, [])

    def test_either_capacity_anchor_requires_the_cli_seal(self) -> None:
        path, digest = _CAPACITY_ANCHORS.items()
        for anchors in ({path[0]: path[1]}, {digest[0]: digest[1]}, dict(_CAPACITY_ANCHORS)):
            with self.subTest(anchors=sorted(anchors)):
                api = _Api(self, anchors=anchors)
                with self.assertRaises(RuntimeError):
                    api.protocol.enter_api_gc_runtime()
        unrelated = _Api(self, anchors={"MINERU_CAPACITY_CONFIG_PATHS": "/elsewhere"})
        unrelated.protocol.enter_api_gc_runtime()
        self.assertEqual(unrelated.log, [])

    def test_managed_seal_requires_linux_cpython_312(self) -> None:
        for interpreter in (
            _Interpreter("darwin"), _Interpreter("win32"), _Interpreter(version=(3, 13, 13)),
            _Interpreter(version=(3, 11, 15)), _Interpreter(implementation="pypy"),
        ):
            with self.subTest(
                platform=interpreter.platform,
                version=interpreter.version_info[:2],
                implementation=interpreter.implementation.name,
            ):
                api = _Api(self, interpreter=interpreter)
                with self.assertRaises(RuntimeError):
                    api.protocol.bootstrap_api_gc(api.app, reload=False)
                self.assertNotIn("hybrid.construct", _kinds(api.log))
                self.assertEqual(_collector_events(api.log), [])
        # A fresh 3.12.13 process already counts its immortals in the permanent
        # generation; that count is not a foreign freeze and must not block the seal.
        api = _Api(self)
        self.assertEqual(api.collector.frozen, _Collector.IMMORTALS)
        api.protocol.bootstrap_api_gc(api.app, reload=False)
        self.assertEqual(api.log.count(("gc.freeze",)), 1)
        self.assertNotIn(("gc.unfreeze",), api.log)
        self.assertEqual(api.collector.frozen, _Collector.SEALED)

    def test_managed_seal_refuses_foreign_owners_before_loading_models(self) -> None:
        # Reload and local VLM preload are refused through the generated CLI in
        # test_managed_options_that_break_the_seal_are_refused_before_models.
        def seal(api: _Api) -> None:
            api.protocol.bootstrap_api_gc(api.app, reload=False)

        def other_thread(api: _Api) -> BaseException | None:
            return _in_thread(lambda: seal(api))

        def child_process(api: _Api) -> BaseException | None:
            spawned = SimpleNamespace(name="SpawnProcess-1")
            with patch.object(multiprocessing, "current_process", return_value=spawned), \
                    patch.object(multiprocessing, "parent_process", return_value=spawned):
                try:
                    seal(api)
                except BaseException as exc:
                    return exc
            return None

        def running_loop(api: _Api) -> BaseException | None:
            async def inside() -> None:
                seal(api)

            try:
                asyncio.run(inside())
            except BaseException as exc:
                return exc
            return None

        def with_state(**state: object) -> Callable[[_Api], BaseException | None]:
            def attempt(api: _Api) -> BaseException | None:
                api.app = _app(**state)  # type: ignore[arg-type]
                try:
                    seal(api)
                except BaseException as exc:
                    return exc
                return None

            return attempt

        def with_capacity(capacity: object) -> Callable[[_Api], BaseException | None]:
            def attempt(api: _Api) -> BaseException | None:
                api.runtime.capacity = capacity
                try:
                    seal(api)
                except BaseException as exc:
                    return exc
                return None

            return attempt

        for name, attempt in (
            ("lifespan thread", other_thread),
            ("spawned child", child_process), ("running loop", running_loop),
            ("existing manager", with_state(manager=object())),
            ("no capacity", with_capacity(None)),
            ("two processes", with_capacity(
                SimpleNamespace(api_process_limit=2, api_event_loop_limit=1))),
            ("two loops", with_capacity(
                SimpleNamespace(api_process_limit=1, api_event_loop_limit=2))),
        ):
            with self.subTest(owner=name):
                api = _Api(self)
                self.assertIsInstance(attempt(api), RuntimeError)
                self.assertNotIn("hybrid.construct", _kinds(api.log))
                self.assertEqual(_collector_events(api.log), [])

        once = _Api(self)
        seal(once)
        with self.assertRaises(RuntimeError):
            seal(once)
        self.assertEqual(once.log.count(("gc.freeze",)), 1)
        self.assertEqual(len(_all(once.log, "hybrid.construct")), 2)

    def test_foreign_collector_state_is_refused_not_repaired(self) -> None:
        def disabled(api: _Api) -> None:
            api.collector.enabled = False

        def save_all(api: _Api) -> None:
            api.collector.debug = gc.DEBUG_SAVEALL

        def old_garbage(api: _Api) -> None:
            api.collector.garbage.append(object())

        def new_garbage(api: _Api) -> None:
            api.collector.after["collect"] = lambda: api.collector.garbage.append(object())

        for name, arrange, before_models in (
            ("automatic GC disabled", disabled, True),
            ("DEBUG_SAVEALL", save_all, True), ("uncollectable garbage", old_garbage, True),
            ("uncollectable after collection", new_garbage, False),
        ):
            with self.subTest(state=name):
                api = _Api(self)
                arrange(api)
                state = (api.collector.enabled, api.collector.frozen, api.collector.debug,
                         api.collector.thresholds)
                with self.assertRaises(RuntimeError):
                    api.protocol.bootstrap_api_gc(api.app, reload=False)
                self.assertNotIn(("gc.freeze",), api.log)
                self.assertNotIn(("gc.unfreeze",), api.log)
                self.assertFalse([event for event in api.log if event[0] in _POLICY_CHANGES])
                self.assertEqual(
                    (api.collector.enabled, api.collector.frozen, api.collector.debug,
                     api.collector.thresholds),
                    state,
                )
                if before_models:
                    self.assertNotIn("hybrid.construct", _kinds(api.log))
                with self.assertRaises(RuntimeError):
                    api.protocol.enter_api_gc_runtime()


class SealBoundaryFailureTests(unittest.TestCase):
    """A seal that fails undoes only its own freeze and never hides the first error."""

    def test_a_failed_seal_undoes_only_its_own_freeze(self) -> None:
        interrupted = RuntimeError("freeze interrupted after moving the heap")

        def freeze_raises(api: _Api) -> object:
            api.collector.failures["freeze"] = interrupted
            return interrupted

        def policy_drifts(api: _Api) -> object:
            api.collector.after["freeze"] = lambda: setattr(api.collector, "thresholds", (5, 5, 5))
            return RuntimeError

        def fact_unwritable(api: _Api) -> object:
            api.stderr.close()
            return ValueError

        for name, arrange in (
            ("freeze interrupted", freeze_raises), ("policy drift at freeze", policy_drifts),
            ("startup fact", fact_unwritable),
        ):
            with self.subTest(failure=name):
                api = _Api(self)
                expected = arrange(api)
                with self.assertRaises(BaseException) as raised:
                    api.protocol.bootstrap_api_gc(api.app, reload=False)
                if isinstance(expected, BaseException):
                    self.assertIs(raised.exception, expected)
                else:
                    self.assertIsInstance(raised.exception, expected)  # type: ignore[arg-type]
                self.assertEqual(api.log.count(("gc.freeze",)), 1)
                self.assertLessEqual(api.collector.frozen, _Collector.IMMORTALS)
                self.assertFalse([event for event in api.log if event[0] in _POLICY_CHANGES])
                with self.assertRaises(RuntimeError):
                    api.protocol.enter_api_gc_runtime()

    def test_cleanup_failures_ride_on_the_first_error(self) -> None:
        stuck = OSError("unfreeze failed during unwinding")
        primary = RuntimeError("freeze interrupted after moving the heap")

        def freeze_raises(api: _Api) -> object:
            api.collector.failures["freeze"] = primary
            return primary

        def fact_unwritable(api: _Api) -> object:
            api.stderr.close()
            return ValueError

        for name, arrange in (("freeze", freeze_raises), ("startup fact", fact_unwritable)):
            with self.subTest(failure=name):
                api = _Api(self)
                expected = arrange(api)
                api.collector.failures["unfreeze"] = stuck
                with self.assertRaises(BaseException) as raised:
                    api.protocol.bootstrap_api_gc(api.app, reload=False)
                if isinstance(expected, BaseException):
                    self.assertIs(raised.exception, expected)
                else:
                    self.assertIsInstance(raised.exception, expected)  # type: ignore[arg-type]
                self.assertIsNot(raised.exception, stuck)
                self.assertTrue(_carries(raised.exception, stuck))
                self.assertEqual(api.collector.frozen, _Collector.SEALED)
                with self.assertRaises(RuntimeError):
                    api.protocol.enter_api_gc_runtime()


class QuiescenceTests(unittest.TestCase):
    """Release waits for proven quiescence; the lifecycle moves one way on one owner."""

    def _sealed(self) -> _Api:
        api = _Api(self)
        api.protocol.bootstrap_api_gc(api.app, reload=False)
        return api

    def test_release_waits_for_quiescence_then_unfreezes_before_dropping_roots(self) -> None:
        api = self._sealed()
        api.protocol.enter_api_gc_runtime()
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                api.protocol.close_api_gc()
            self.assertNotIn("gc.unfreeze", _kinds(api.log))
            self.assertNotIn("model.released", _kinds(api.log))
            self.assertEqual(api.collector.frozen, _Collector.SEALED)
        api.protocol.mark_api_gc_quiesced()
        api.protocol.close_api_gc()
        release = _first(api.log, "gc.unfreeze")
        released = _all(api.log, "model.released")
        self.assertEqual(len(released), len(api.runtime.static))
        self.assertLess(release, min(released))
        self.assertLess(max(released), _last(api.log, ("gc.collect", 2)))
        self.assertEqual(api.collector.frozen, _Collector.IMMORTALS)
        self.assertEqual((api.collector.enabled, api.collector.thresholds), (True, _THRESHOLDS))
        self.assertEqual(api.runtime.hybrid._models, {})
        self.assertEqual(api.runtime.atom._models, {})
        settled = len(api.log)
        api.protocol.close_api_gc()
        self.assertEqual(
            [event for event in api.log[settled:] if event[0] != "stderr"], []
        )

    def test_lifecycle_moves_one_way_on_its_owner_thread(self) -> None:
        api = self._sealed()
        with self.assertRaises(RuntimeError):
            api.protocol.mark_api_gc_quiesced()
        self.assertIsInstance(_in_thread(api.protocol.enter_api_gc_runtime), RuntimeError)
        api.protocol.enter_api_gc_runtime()
        with self.assertRaises(RuntimeError):
            api.protocol.enter_api_gc_runtime()
        self.assertIsInstance(_in_thread(api.protocol.mark_api_gc_quiesced), RuntimeError)
        api.protocol.mark_api_gc_quiesced()
        self.assertIsInstance(_in_thread(api.protocol.close_api_gc), RuntimeError)
        self.assertNotIn("gc.unfreeze", _kinds(api.log))
        api.protocol.close_api_gc()
        with self.assertRaises(RuntimeError):
            api.protocol.enter_api_gc_runtime()
        with self.assertRaises(RuntimeError):
            api.protocol.mark_api_gc_quiesced()

    def test_a_failed_release_is_not_reported_closed(self) -> None:
        api = self._sealed()
        api.protocol.enter_api_gc_runtime()
        api.protocol.mark_api_gc_quiesced()
        failure = OSError("unfreeze failed at shutdown")
        api.collector.failures["unfreeze"] = failure
        with self.assertRaises(OSError) as raised:
            api.protocol.close_api_gc()
        self.assertIs(raised.exception, failure)
        self.assertEqual(api.collector.frozen, _Collector.SEALED)
        self.assertEqual(len(_facts(api.log)), 1)
        with self.assertRaises(RuntimeError):
            api.protocol.enter_api_gc_runtime()


class _Request:
    """A request-scoped object that owns futures and a back reference."""


async def _serve_requests() -> list[weakref.ref[_Request]]:
    loop = asyncio.get_running_loop()
    references: list[weakref.ref[_Request]] = []
    for index in range(32):
        request = _Request()
        request.me = request  # type: ignore[attr-defined]
        request.future = loop.create_future()  # type: ignore[attr-defined]
        request.future.add_done_callback(lambda _future, owner=request: owner)  # type: ignore[attr-defined]
        request.future.set_result(index)  # type: ignore[attr-defined]
        await asyncio.sleep(0)
        references.append(weakref.ref(request))
        del request
    return references


def _child_parts() -> tuple[ModuleType, _ModelRuntime, list[Event]]:
    log: list[Event] = []
    protocol = _fresh_protocol()
    protocol.sys = _Interpreter()  # type: ignore[attr-defined]
    runtime = _ModelRuntime(log, device="cpu", cyclic=True)
    _install_modules(_with_packages(runtime.modules), None)
    os.environ.update(_CAPACITY_ANCHORS)
    return protocol, runtime, log


def _settled_freeze_count() -> int:
    """The permanent generation after a full collection: CPython 3.12 immortals only."""
    gc.collect()
    return gc.get_freeze_count()


def _child_lifecycle() -> dict[str, Any]:
    protocol, runtime, _ = _child_parts()
    policy = (gc.isenabled(), gc.get_threshold())
    seal_facts = io.StringIO()
    with redirect_stderr(seal_facts):
        protocol.bootstrap_api_gc(_app(), reload=False)
    sealed = gc.get_freeze_count()
    static = list(runtime.static)
    protocol.enter_api_gc_runtime()

    requests = asyncio.run(_serve_requests())
    fresh = _Request()
    fresh.me = fresh  # type: ignore[attr-defined]
    fresh_reference = weakref.ref(fresh)
    del fresh
    model = runtime.hybrid._models[(None, True)]
    orphan, model.cache = model.cache, []
    crossing = _Request()
    orphan.append(crossing)
    crossing.cache = orphan  # type: ignore[attr-defined]
    crossing_reference = weakref.ref(crossing)
    del model, orphan, crossing
    gc.collect()
    verdict: dict[str, Any] = {
        "sealed": sealed,
        "freeze_count_while_serving": gc.get_freeze_count(),
        "requests_reclaimed_while_sealed": all(reference() is None for reference in requests),
        "fresh_cycle_reclaimed_while_sealed": fresh_reference() is None,
        "static_alive_while_sealed": all(reference() is not None for reference in static),
        "crossing_retained_while_sealed": crossing_reference() is not None,
    }
    protocol.mark_api_gc_quiesced()
    with redirect_stderr(io.StringIO()):
        protocol.close_api_gc()
    verdict.update({
        "crossing_reclaimed_by_release": crossing_reference() is None,
        "static_reclaimed_by_release": bool(static) and all(r() is None for r in static),
        "freeze_count_after_release": gc.get_freeze_count(),
        "policy_kept": (gc.isenabled(), gc.get_threshold()) == policy,
        "facts": [
            line for line in seal_facts.getvalue().splitlines() if line.startswith(_MARKER_PREFIX)
        ],
        "pid": os.getpid(),
        "thresholds": list(policy[1]),
    })
    return verdict


def _child_failures() -> dict[str, Any]:
    verdict: dict[str, Any] = {}
    baseline = _settled_freeze_count()
    protocol, runtime, _ = _child_parts()
    failure = OSError("hybrid weights unavailable")
    runtime.construct_failure = failure
    try:
        protocol.bootstrap_api_gc(_app(), reload=False)
    except OSError as exc:
        verdict["construction_error_kept"] = exc is failure
    verdict["construction_left_no_seal"] = _settled_freeze_count() <= baseline

    protocol, runtime, _ = _child_parts()
    closed = io.StringIO()
    closed.close()
    try:
        with redirect_stderr(closed):
            protocol.bootstrap_api_gc(_app(), reload=False)
    except ValueError:
        verdict["unwritable_fact_raised"] = True
    verdict["unwritable_left_no_seal"] = _settled_freeze_count() <= baseline
    try:
        protocol.enter_api_gc_runtime()
    except RuntimeError:
        verdict["unwritable_refuses_serving"] = True
    return verdict


def _child_fork() -> dict[str, Any]:
    baseline = _settled_freeze_count()
    protocol, _, _ = _child_parts()
    with redirect_stderr(io.StringIO()):
        protocol.bootstrap_api_gc(_app(), reload=False)
    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            protocol.enter_api_gc_runtime()
            status |= 1
        except RuntimeError:
            pass
        try:
            with redirect_stderr(io.StringIO()):
                protocol.close_api_gc()
        except RuntimeError:
            pass
        if gc.get_freeze_count() <= baseline:
            status |= 2
        os._exit(status)
    _, wait_status = os.waitpid(pid, 0)
    protocol.enter_api_gc_runtime()
    protocol.mark_api_gc_quiesced()
    with redirect_stderr(io.StringIO()):
        protocol.close_api_gc()
    return {
        "inherited_status": os.waitstatus_to_exitcode(wait_status),
        "owner_released": _settled_freeze_count() <= baseline,
    }


def _child_render_manager() -> dict[str, Any]:
    """A real spawn pool whose worker is already dead while its manager still cleans up."""
    from concurrent.futures import ProcessPoolExecutor

    log: list[Event] = []
    protocol = _fresh_protocol()
    render = _render_tools(log, lambda: None)
    _install_modules(_with_packages({"mineru.utils.pdf_image_tools": render}), None)
    code, imports = _Generated.load()
    namespace: dict[str, Any] = {
        local: getattr(protocol, name) for local, name in imports if hasattr(protocol, name)
    }
    namespace.update({
        "FastAPI": object,
        "shutdown_cached_models": lambda: None,
        "shutdown_pdf_render_executor": render.shutdown_pdf_render_executor,
        "logger": SimpleNamespace(warning=lambda message: None, info=lambda message: None),
    })
    exec(code, namespace)

    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    verdict: dict[str, Any] = {"warm": pool.submit(abs, -1).result(timeout=60) == 1}
    manager = pool._executor_manager_thread  # type: ignore[attr-defined]
    workers = list(pool._processes.values())  # type: ignore[attr-defined]
    release = threading.Event()

    def held(step: Callable[..., Any]) -> Callable[..., Any]:
        def hold(*args: Any, **kwargs: Any) -> Any:
            release.wait(60)
            return step(*args, **kwargs)

        return hold

    # Every exit of the manager loop passes one of these; hold it inside its cleanup.
    for step in ("terminate_broken", "join_executor_internals"):
        setattr(manager, step, held(getattr(manager, step)))
    for worker in workers:
        worker.terminate()
        worker.join(30)
    verdict["workers_dead_before_stop"] = not any(worker.is_alive() for worker in workers)
    render._pdf_render_executor = pool
    try:
        namespace["shutdown_runtime_resources"]()
    except BaseException as exc:
        verdict["stop_failed_visibly"] = True
        verdict["failure"] = type(exc).__name__
    else:
        verdict["stop_failed_visibly"] = False
    verdict["manager_alive_at_verdict"] = manager.is_alive()
    verdict["manager_reference_dropped"] = pool._executor_manager_thread is None  # type: ignore[attr-defined]
    release.set()
    manager.join(60)
    verdict["manager_reaped"] = not manager.is_alive()
    verdict["workers_reaped"] = all(worker.exitcode is not None for worker in workers)
    return verdict


_CHILD_SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "lifecycle": _child_lifecycle,
    "failures": _child_failures,
    "fork": _child_fork,
    "render-manager": _child_render_manager,
}


def _child_main(scenario: str) -> None:
    """Child-interpreter entry; prints one JSON verdict line."""
    sys.stdout.write(json.dumps(_CHILD_SCENARIOS[scenario](), sort_keys=True) + "\n")


class RealCollectorTests(unittest.TestCase):
    """The lifecycle against CPython's own collector and process pool, in child interpreters."""

    def _child(self, scenario: str) -> dict[str, Any]:
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("MINERU_")
        }
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD_BOOTSTRAP, str(_SERVICE_ROOT), scenario],
            capture_output=True, text=True, timeout=120, check=False, env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_request_objects_stay_collectable_and_cross_frozen_cycles_wait_for_release(
        self,
    ) -> None:
        verdict = self._child("lifecycle")
        self.assertGreater(verdict["sealed"], 0)
        # Frozen objects may still die by reference count; nothing joins after the seal.
        self.assertLessEqual(verdict["freeze_count_while_serving"], verdict["sealed"])
        self.assertTrue(verdict["requests_reclaimed_while_sealed"])
        self.assertTrue(verdict["fresh_cycle_reclaimed_while_sealed"])
        self.assertTrue(verdict["static_alive_while_sealed"])
        self.assertTrue(verdict["crossing_retained_while_sealed"])
        self.assertTrue(verdict["crossing_reclaimed_by_release"])
        self.assertTrue(verdict["static_reclaimed_by_release"])
        # On 3.12 the immortals return after the release collection; the seal does not.
        self.assertLess(verdict["freeze_count_after_release"], verdict["sealed"])
        self.assertTrue(verdict["policy_kept"])
        self.assertEqual(len(verdict["facts"]), 1, verdict["facts"])
        fact = json.loads(verdict["facts"][0][len(_MARKER_PREFIX):])
        self.assertLessEqual(_MARKER_REQUIRED, set(fact))
        self.assertLessEqual(set(fact), _MARKER_ALLOWED)
        self.assertEqual(fact["pid"], verdict["pid"])
        self.assertGreaterEqual(fact["freeze_count"], verdict["sealed"])
        self.assertIs(fact["automatic_gc"], True)
        self.assertEqual(fact["thresholds"], verdict["thresholds"])

    def test_failed_seals_leave_no_frozen_heap_behind(self) -> None:
        self.assertEqual(self._child("failures"), {
            "construction_error_kept": True,
            "construction_left_no_seal": True,
            "unwritable_fact_raised": True,
            "unwritable_left_no_seal": True,
            "unwritable_refuses_serving": True,
        })

    @unittest.skipUnless(hasattr(os, "fork"), "requires os.fork")
    def test_a_forked_child_cannot_drive_the_inherited_epoch(self) -> None:
        self.assertEqual(
            self._child("fork"), {"inherited_status": 0, "owner_released": True}
        )

    def test_a_real_pool_manager_still_cleaning_up_blocks_the_final_stop(self) -> None:
        self.assertEqual(self._child("render-manager"), {
            "warm": True,
            "workers_dead_before_stop": True,
            "stop_failed_visibly": True,
            "failure": "RuntimeError",
            "manager_alive_at_verdict": True,
            "manager_reference_dropped": True,
            "manager_reaped": True,
            "workers_reaped": True,
        })


if __name__ == "__main__":
    unittest.main()
