"""Independent reclaim decisions/lifecycle checks; no torch/vLLM/GPU imports."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import gc
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from tests.unit.test_mineru_owner_memory_independent import core, fake_worker, monitor, scheduler


_PACKAGE = Path(__file__).resolve().parents[2] / "scripts/windows/mineru_inference_memory"
_PARENT = "_independent_mineru_reclaim"
_package = ModuleType(_PARENT)
_package.__path__ = [str(_PACKAGE)]
_spec = importlib.util.spec_from_file_location(_PARENT + ".reclaim", _PACKAGE / "reclaim.py")
assert _spec is not None and _spec.loader is not None
reclaim = importlib.util.module_from_spec(_spec)
with patch.dict(sys.modules, {_PARENT: _package, _PARENT + ".core": core}):
    _spec.loader.exec_module(reclaim)


class PoolCuda:
    """Only ordinary free cache can release; live/private-pool bytes stay put.

    This is a decision oracle, not a simulation or proof of the real allocator.
    """

    def __init__(self, *, ordinary=700):
        self.calls = []
        self.ordinary = ordinary
        self.initial_ordinary = ordinary
        self.graph_inactive = 500
        self.split = 300
        self.protected = {name: object() for name in ("weights", "kv", "graph", "pending_output")}
        self.pending = SimpleNamespace(completed=False, output_reads=0)
        self.initialized = True
        self.device = 0
        self.capturing = False
        self.backend = "native"
        self.frees = 12
        self.failure = None
        self.measurements = 0
        self.after_unknown = False
        self.external_free_change = 0

    def is_initialized(self):
        self.calls.append("is_initialized")
        return self.initialized

    def current_device(self):
        self.calls.append("current_device")
        return self.device

    def is_current_stream_capturing(self):
        self.calls.append("capturing")
        return self.capturing

    def allocator_backend(self):
        self.calls.append("backend")
        return self.backend

    def memory_stats(self, device):
        self.calls.append(("stats", device))
        self.measurements += 1
        if self.after_unknown and "empty_cache" in self.calls:
            return {}
        stats = {key: 0 for key in core.ALLOCATOR_KEYS}
        reserved = 6000 + self.graph_inactive + self.split + self.ordinary
        for metric in ("current", "peak"):
            stats["allocated_bytes.all." + metric] = 6000
            stats["active_bytes.all." + metric] = 6000
            stats["reserved_bytes.all." + metric] = reserved
            stats["inactive_split_bytes.all." + metric] = self.split
        stats["segment.all.current"] = 7 + (self.ordinary > 0)
        stats["num_device_free"] = self.frees
        return stats

    def mem_get_info(self, device):
        self.calls.append(("mem_info", device))
        return (1000 + self.initial_ordinary - self.ordinary + self.external_free_change, 20_000)

    def synchronize(self, device):
        self.calls.append(("synchronize", device))
        self.pending.completed = True
        if self.failure and self.failure[0] == "synchronize":
            raise self.failure[1]

    def empty_cache(self):
        self.calls.append("empty_cache")
        if self.failure and self.failure[0] == "empty_cache":
            raise self.failure[1]
        if self.ordinary:
            self.frees += 1
            self.ordinary = 0

    def __getattr__(self, name):
        raise AssertionError("unapproved CUDA operation: " + name)


def ready(*, ordinary=700, last_step=None, requests=None):
    observer, _, state, records, runner = monitor()
    cuda = PoolCuda(ordinary=ordinary)
    observer._cuda = cuda
    runner.requests = {} if requests is None else requests
    observer.after_warmup()
    if last_step == "work":
        observer.before_execute(scheduler(tokens=1))
        observer.after_execute(b"pending-async-output")
    elif last_step == "cleanup":
        observer.before_execute(scheduler())
        observer.after_execute(())
    state.now = reclaim.RECLAIM_IDLE_NS
    cuda.calls.clear()
    actor = reclaim.OwnerMemoryReclaimer(monitor=observer, runner=runner, cuda=cuda,
        logger=observer._logger, clock_ns=lambda: state.now, wall_ns=lambda: 1)
    return actor, observer, cuda, state, records, runner


class IndependentReclaimOwnerTests(unittest.TestCase):
    def test_owner_silence_is_cadence_and_local_request_state_is_only_diagnostic(self):
        output = object()
        actor, _, cuda, _, records, runner = ready(last_step="work", requests={"pending": output})
        protected = dict(cuda.protected)
        answer = actor.attempt()
        self.assertEqual(answer["outcome"], "released")
        self.assertEqual(answer["released_reserved_bytes"], 700)
        self.assertEqual(cuda.protected, protected)
        self.assertIs(runner.requests["pending"], output)
        self.assertTrue(cuda.pending.completed)
        self.assertEqual(cuda.pending.output_reads, 0)
        event = [r for r in records if r["kind"] == "reclaim"][-1]
        self.assertEqual(event["gate"]["last_step"], "work")
        self.assertEqual(event["device_work"], "synchronized")
        self.assertNotIn("globally_idle", event["gate"])

    def test_protected_graph_residue_is_no_benefit_instead_of_subtraction_yield(self):
        actor, _, cuda, _, records, _ = ready(ordinary=0)
        before = dict(cuda.protected)
        answer = actor.attempt()
        self.assertEqual(answer["outcome"], "no_benefit")
        self.assertEqual(answer["released_reserved_bytes"], 0)
        self.assertEqual(cuda.protected, before)
        event = [r for r in records if r["kind"] == "reclaim"][-1]
        stats = event["before"]["allocator"]["stats"]
        self.assertEqual(stats["reserved_bytes.all.current"] - stats["active_bytes.all.current"]
                         - stats["inactive_split_bytes.all.current"], 500)
        self.assertEqual(event["effect"]["released_reserved_bytes"], 0)
        self.assertEqual(event["effect"]["device_free_calls"], 0)
        for _ in range(20):
            self.assertEqual(actor.attempt()["outcome"], "refused")
        self.assertEqual(cuda.calls.count("empty_cache"), 1)

    def test_release_is_measured_once_and_regrowth_requires_a_new_quiet_episode(self):
        actor, observer, cuda, state, records, _ = ready()
        state.now -= 1
        self.assertEqual(actor.attempt()["outcome"], "refused")
        self.assertEqual(cuda.calls, [])
        state.now += 1
        self.assertEqual(actor.attempt()["outcome"], "released")
        self.assertLess(cuda.calls.index(("synchronize", 0)), cuda.calls.index("empty_cache"))
        for _ in range(20):
            actor.attempt()
        self.assertEqual(cuda.calls.count("empty_cache"), 1)
        self.assertEqual(len([r for r in records if r["kind"] == "reclaim"]), 1)
        cuda.ordinary = 250
        observer.before_execute(scheduler(tokens=1))
        observer.after_execute(("final",))
        state.now += reclaim.RECLAIM_IDLE_NS - 1
        self.assertEqual(actor.attempt()["outcome"], "refused")
        state.now += 1
        self.assertEqual(actor.attempt()["released_reserved_bytes"], 250)
        self.assertEqual(cuda.calls.count("empty_cache"), 2)

    def test_foreign_owner_uninitialized_context_wrong_device_and_capture_are_closed(self):
        for kind in ("pid", "thread", "uninitialized", "wrong_device", "capturing"):
            actor, _, cuda, state, _, _ = ready()
            if kind in ("pid", "thread"):
                setattr(state, kind, 99)
            elif kind == "uninitialized":
                cuda.initialized = False
            elif kind == "wrong_device":
                cuda.device = 1
            else:
                cuda.capturing = True
            with self.subTest(kind=kind):
                self.assertEqual(actor.attempt()["outcome"], "failed")
                self.assertNotIn("empty_cache", cuda.calls)
                self.assertFalse(any(isinstance(c, tuple) and c[0] == "synchronize" for c in cuda.calls))
                if kind in ("pid", "thread"):
                    self.assertEqual(cuda.calls, [])
                if kind == "uninitialized":
                    self.assertEqual(cuda.calls, ["is_initialized"])
                self.assertEqual(actor.attempt()["outcome"], "disabled")

    def test_faults_propagate_exactly_latch_off_and_unknown_effect_is_never_success(self):
        for stage in ("synchronize", "empty_cache"):
            actor, _, cuda, _, records, _ = ready()
            failure = RuntimeError("private CUDA fault detail")
            cuda.failure = stage, failure
            with self.subTest(stage=stage), self.assertRaises(RuntimeError) as caught:
                actor.attempt()
            self.assertIs(caught.exception, failure)
            self.assertEqual(actor.attempt()["outcome"], "disabled")
            event = [r for r in records if r["kind"] == "reclaim"][-1]
            self.assertEqual((event["outcome"], event["reason"], event["error_type"]),
                             ("failed", stage, "RuntimeError"))
            self.assertIsNone(event["effect"])
            self.assertNotIn("private CUDA fault detail", str(event))
        actor, _, cuda, _, records, _ = ready()
        cuda.after_unknown = True
        self.assertEqual(actor.attempt()["outcome"], "failed")
        self.assertEqual(cuda.ordinary, 0)
        self.assertEqual(actor.attempt()["outcome"], "disabled")
        self.assertIsNone([r for r in records if r["kind"] == "reclaim"][-1]["effect"])

    def test_engine_source_and_topology_binding_fail_before_any_cuda_and_latch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules = {}
            digests = {}
            for relative, (name, expected) in reclaim.RECLAIM_ENGINE_SOURCES.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# independent source stand-in")
                module = ModuleType(name)
                module.__file__ = str(path)
                modules[name] = module
                digests[path] = expected
            topology = dict(distributed_executor_backend="uni", world_size=1, data_parallel_size=1)
            for case in ("valid", "wrong_hash", "wrong_loaded_file", "multiprocess", "tensor_parallel"):
                with self.subTest(case=case), patch.dict(sys.modules, modules):
                    selected = dict(digests)
                    parallel = SimpleNamespace(**topology)
                    module = modules[next(iter(modules))]
                    correct_file = module.__file__
                    if case == "wrong_hash":
                        selected[next(iter(selected))] = "f" * 64
                    elif case == "wrong_loaded_file":
                        module.__file__ = str(root / "other-loaded-copy.py")
                    elif case == "multiprocess":
                        parallel.distributed_executor_backend = "mp"
                    elif case == "tensor_parallel":
                        parallel.world_size = 2
                    actor, _, cuda, _, records, _ = ready()
                    calls = []
                    def binding():
                        calls.append(True)
                        return reclaim.engine_binding(root, parallel)
                    actor._binding = binding
                    try:
                        with patch.object(reclaim, "file_sha256", side_effect=lambda path:
                            selected[path] if path in selected else "e" * 64):
                            reply = actor.attempt()
                            if case == "valid":
                                self.assertEqual(reply["outcome"], "released")
                                actor.attempt()
                                self.assertEqual(calls, [True])
                                self.assertEqual(len([r for r in records if r["kind"] == "bound"]), 1)
                            else:
                                self.assertEqual(reply["outcome"], "failed")
                                self.assertEqual(reply["reason"], "engine_source" if case in
                                    ("wrong_hash", "wrong_loaded_file") else "executor_topology")
                                self.assertEqual(cuda.calls, [])
                                self.assertEqual(actor.attempt()["outcome"], "disabled")
                    finally:
                        module.__file__ = correct_file

    def test_clock_regression_and_unsupported_allocator_do_not_mutate(self):
        actor, _, cuda, state, _, _ = ready()
        state.now = -1
        self.assertEqual(actor.attempt()["outcome"], "refused")
        self.assertEqual(cuda.calls, [])
        actor, _, cuda, _, _, _ = ready()
        cuda.backend = "cudaMallocAsync"
        self.assertEqual(actor.attempt()["outcome"], "failed")
        self.assertNotIn("empty_cache", cuda.calls)
        self.assertNotIn(("synchronize", 0), cuda.calls)


class IndependentReclaimHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def logger(self):
        from tests.unit.test_mineru_owner_memory_independent import Records
        records = Records()
        logger = logging.Logger("independent-reclaim-heartbeat", logging.INFO)
        logger.addHandler(records)
        return logger, records.values

    async def test_unanswered_rpc_stops_replacement_without_claiming_owner_cancellation(self):
        logger, records = self.logger()
        owner_finished = asyncio.Event()
        owner_started = asyncio.Event()
        async def owner():
            owner_started.set()
            await owner_finished.wait()
            return [{"schema": reclaim.RECLAIM_SCHEMA, "outcome": "no_benefit"}]
        owner_task = asyncio.create_task(owner())
        calls = []
        class Client:
            errored = False
            async def collective_rpc(self, method):
                calls.append(method)
                return await asyncio.shield(owner_task)
        async def immediate(_):
            await asyncio.sleep(0)
        try:
            result = await asyncio.wait_for(reclaim.run_heartbeat(Client(), logger=logger,
                interval=0, timeout=0.01, sleep=immediate), 0.5)
            self.assertIn("unknown", result)
            self.assertTrue(owner_started.is_set())
            self.assertFalse(owner_task.done())
            self.assertEqual(calls, [core.RECLAIM_METHOD])
            self.assertTrue(any("unknown" in str(r) for r in records))
        finally:
            owner_finished.set()
            await owner_task

    async def test_abandoned_local_rpc_late_exception_is_observed_without_another_submit(self):
        # Cancellation can close the local await but cannot prove the owner ended.
        # A client coroutine may finish cancellation cleanup later; its failure
        # still belongs to the heartbeat and must have a terminal observer.
        logger, _ = self.logger()
        cancelled, finish = asyncio.Event(), asyncio.Event()
        calls, tasks, unhandled = [], [], []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        class Client:
            errored = False
            async def collective_rpc(self, method):
                calls.append(method)
                tasks.append(asyncio.current_task())
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish.wait()
                    raise RuntimeError("late client cleanup fault")
        async def immediate(_):
            await asyncio.sleep(0)
        try:
            result = await asyncio.wait_for(reclaim.run_heartbeat(Client(), logger=logger,
                interval=0, timeout=0.01, sleep=immediate), 0.5)
            self.assertEqual(result, "outcome_unknown")
            await asyncio.wait_for(cancelled.wait(), 0.5)
            finish.set()
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(calls, [core.RECLAIM_METHOD])
            self.assertTrue(tasks[0].done())
            # Drop only the test's ownership; an unobserved terminal exception
            # now reaches the public loop exception handler when collected.
            tasks.clear()
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
        finally:
            finish.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            loop.set_exception_handler(old_handler)

    async def test_same_tick_rpc_fault_and_heartbeat_cancellation_observe_already_done_call(self):
        # This RPC is already done when the heartbeat handles cancellation,
        # unlike the delayed cancellation-cleanup case above. Observe the
        # terminal failure without replacing the call or requiring native wait.
        logger, _ = self.logger()
        loop = asyncio.get_running_loop()
        unhandled, calls, tasks = [], [], []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        heartbeat = None
        class Client:
            errored = False
            async def collective_rpc(self, method):
                calls.append(method)
                tasks.append(asyncio.current_task())
                loop.call_soon(heartbeat.cancel)
                raise RuntimeError("same-tick client fault")
        async def immediate(_):
            await asyncio.sleep(0)
        try:
            heartbeat = asyncio.create_task(reclaim.run_heartbeat(Client(), logger=logger,
                interval=0, timeout=20, sleep=immediate))
            with self.assertRaises(asyncio.CancelledError):
                await heartbeat
            self.assertEqual(calls, [core.RECLAIM_METHOD])
            self.assertTrue(tasks[0].done())
            # Permit an already-done future's callback to run, then drop test
            # ownership so the public loop handler reveals unconsumed faults.
            for _ in range(4):
                await asyncio.sleep(0)
            tasks.clear()
            heartbeat = None
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [], "already-done RPC fault escaped terminal observation")
        finally:
            if heartbeat is not None and not heartbeat.done():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            for task in tasks:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            loop.set_exception_handler(old_handler)

    async def test_cancelling_heartbeat_closes_local_wait_and_never_resubmits_or_cancels_owner(self):
        logger, _ = self.logger()
        owner_done, local_started, local_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def owner():
            await owner_done.wait()
            return [{"schema": reclaim.RECLAIM_SCHEMA, "outcome": "no_benefit"}]
        owner_task = asyncio.create_task(owner())
        calls = []
        class Client:
            errored = False
            async def collective_rpc(self, method):
                calls.append(method)
                local_started.set()
                try:
                    return await asyncio.shield(owner_task)
                finally:
                    local_closed.set()
        async def immediate(_):
            await asyncio.sleep(0)
        heartbeat = asyncio.create_task(reclaim.run_heartbeat(Client(), logger=logger,
            interval=0, timeout=20, sleep=immediate))
        try:
            await asyncio.wait_for(local_started.wait(), 0.5)
            heartbeat.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await heartbeat
            await asyncio.wait_for(local_closed.wait(), 0.5)
            self.assertFalse(owner_task.done())
            self.assertEqual(calls, [core.RECLAIM_METHOD])
        finally:
            owner_done.set()
            await owner_task
            if not heartbeat.done():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def test_success_refusal_and_no_benefit_are_silent_until_a_terminal_answer(self):
        logger, records = self.logger()
        replies = iter([{"outcome": "refused", "reason": "busy"}, {"outcome": "released"},
                        {"outcome": "no_benefit"}, {"outcome": "disabled"}])
        calls = []
        class Client:
            errored = False
            async def collective_rpc(self, method):
                calls.append(method)
                return [{"schema": reclaim.RECLAIM_SCHEMA, **next(replies)}]
        async def immediate(_):
            await asyncio.sleep(0)
        result = await reclaim.run_heartbeat(Client(), logger=logger, interval=0,
                                             timeout=0.1, sleep=immediate)
        self.assertEqual(result, "owner_disabled")
        self.assertEqual(len(calls), 4)
        self.assertEqual([r["kind"] for r in records], ["heartbeat_start", "heartbeat_stop"])

    async def test_unknown_reply_outcome_is_terminal_malformed_not_a_success(self):
        logger, records = self.logger()
        class Client:
            calls = 0
            @property
            def errored(self):
                return self.calls > 0
            async def collective_rpc(self, method):
                self.calls += 1
                return [{"schema": reclaim.RECLAIM_SCHEMA, "outcome": "invented-success"}]
        async def immediate(_):
            await asyncio.sleep(0)
        client = Client()
        result = await reclaim.run_heartbeat(client, logger=logger, interval=0,
                                             timeout=0.1, sleep=immediate)
        self.assertEqual(result, "malformed_reply")
        self.assertEqual(client.calls, 1)
        self.assertEqual(records[-1]["reason"], "malformed_reply")

    async def test_heartbeat_reaches_bound_owner_without_a_new_request_or_worker_hook(self):
        logger, _ = self.logger()
        actor, observer, cuda, _, _, _ = ready()
        with fake_worker() as (module, _):
            worker = module.OwnerMemoryWorker.__new__(module.OwnerMemoryWorker)
        worker._mineru_owner_reclaimer = actor
        class Client:
            calls = 0
            @property
            def errored(self):
                return self.calls > 0
            async def collective_rpc(self, method):
                self.calls += 1
                return [getattr(worker, method)()]
        async def immediate(_):
            await asyncio.sleep(0)
        client = Client()
        self.assertIsNone(observer.idle_evidence().last_step)
        self.assertEqual(await reclaim.run_heartbeat(client, logger=logger,
            interval=0, timeout=0.1, sleep=immediate), "engine_errored")
        self.assertEqual(client.calls, 1)
        self.assertEqual(cuda.calls.count("empty_cache"), 1)
        self.assertEqual(cuda.ordinary, 0)
        self.assertIsNone(observer.idle_evidence().last_step)

    async def test_lifespan_owns_one_heartbeat_and_joins_it_before_shutdown_delivery(self):
        started, joined = asyncio.Event(), asyncio.Event()
        calls, observed = [], []
        async def owned(engine):
            calls.append(engine)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()
        engine = SimpleNamespace(collective_rpc=lambda _: None)
        scope = {"type": "lifespan", "app": SimpleNamespace(state=SimpleNamespace(engine_client=engine))}
        startup, shutdown = {"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}
        incoming = iter([startup, shutdown])
        async def receive():
            return next(incoming)
        async def send(message):
            observed.append(message)
        async def app(app_scope, app_receive, app_send):
            self.assertIs(app_scope, scope)
            self.assertIs(await app_receive(), startup)
            complete = {"type": "lifespan.startup.complete"}
            await app_send(complete)
            await app_send(complete)
            await asyncio.wait_for(started.wait(), 0.5)
            self.assertIs(await app_receive(), shutdown)
            self.assertTrue(joined.is_set())
            await app_send({"type": "lifespan.shutdown.complete"})
        middleware = reclaim.OwnerMemoryReclaimMiddleware(app)
        with patch.object(reclaim, "_supervised_heartbeat", owned):
            await asyncio.wait_for(middleware(scope, receive, send), 0.5)
        self.assertEqual(calls, [engine])
        self.assertIsNone(middleware._task)
        self.assertEqual(observed[-1], {"type": "lifespan.shutdown.complete"})

    async def test_lifespan_exception_preserves_exception_and_closes_owned_heartbeat(self):
        started, joined = asyncio.Event(), asyncio.Event()
        failure = LookupError("original lifespan failure")
        async def owned(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()
        scope = {"type": "lifespan", "app": SimpleNamespace(state=SimpleNamespace(
            engine_client=SimpleNamespace(collective_rpc=lambda _: None)))}
        async def app(_scope, _receive, send):
            await send({"type": "lifespan.startup.complete"})
            await asyncio.wait_for(started.wait(), 0.5)
            raise failure
        async def receive():
            return {"type": "lifespan.startup"}
        async def send(_):
            pass
        middleware = reclaim.OwnerMemoryReclaimMiddleware(app)
        try:
            with patch.object(reclaim, "_supervised_heartbeat", owned):
                with self.assertRaises(LookupError) as caught:
                    await middleware(scope, receive, send)
            self.assertIs(caught.exception, failure)
            self.assertTrue(joined.is_set(), "lifespan failure orphaned the owned heartbeat")
            self.assertIsNone(middleware._task)
        finally:
            task = middleware._task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_lifespan_cancellation_closes_owned_heartbeat_without_swallowing_cancellation(self):
        started, joined = asyncio.Event(), asyncio.Event()
        async def owned(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()
        async def app(_scope, _receive, send):
            await send({"type": "lifespan.startup.complete"})
            await asyncio.Event().wait()
        async def receive():
            return {"type": "lifespan.startup"}
        async def send(_):
            pass
        scope = {"type": "lifespan", "app": SimpleNamespace(state=SimpleNamespace(
            engine_client=SimpleNamespace(collective_rpc=lambda _: None)))}
        middleware = reclaim.OwnerMemoryReclaimMiddleware(app)
        try:
            with patch.object(reclaim, "_supervised_heartbeat", owned):
                task = asyncio.create_task(middleware(scope, receive, send))
                await asyncio.wait_for(started.wait(), 0.5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(joined.is_set(), "lifespan cancellation orphaned the heartbeat")
            self.assertIsNone(middleware._task)
        finally:
            if middleware._task is not None:
                middleware._task.cancel()
                await asyncio.gather(middleware._task, return_exceptions=True)

    async def test_middleware_preserves_http_and_websocket_objects_and_routes(self):
        for kind in ("http", "websocket"):
            scope = {"type": kind, "path": "/existing-route"}
            incoming, outgoing = {"type": "input", "body": b"unchanged"}, {"type": "output"}
            observed = []
            async def receive():
                return incoming
            async def send(message):
                self.assertIs(message, outgoing)
                observed.append(message)
            async def app(app_scope, app_receive, app_send):
                self.assertIs(app_scope, scope)
                self.assertIs(await app_receive(), incoming)
                await app_send(outgoing)
            middleware = reclaim.OwnerMemoryReclaimMiddleware(app)
            await middleware(scope, receive, send)
            self.assertEqual(observed, [outgoing])
            self.assertIsNone(middleware._task)


if __name__ == "__main__":
    unittest.main()
