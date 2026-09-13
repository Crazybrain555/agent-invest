"""Bound serving HTTP ownership through actual generated POST and real loops."""

import asyncio
import os
import threading
import unittest
from unittest import mock
import uuid

from tests._mineru_capacity_http_fixture import (
    CONFIG_SHA256,
    HttpFixture,
    Transport,
    foreign_call,
    generated_http,
    loop_fence,
)


class CapacityHttpOwnerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = generated_http()

    async def asyncSetUp(self):
        self.environment = mock.patch.dict(os.environ, {
            "MINERU_CAPACITY_CONFIG_PATH": "/independent/config.json",
            "MINERU_CAPACITY_CONFIG_SHA256": CONFIG_SHA256,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.fx = HttpFixture(self.source)
        self.callbacks = []
        self.callback = lambda: self.callbacks.append(threading.get_ident())

    async def test_binding_and_snapshot_are_lazy_and_return_detached_values(self):
        self.fx.bind(self.callback)
        first = self.fx.snapshot()
        self.assertEqual(set(first), {"process_id", "loop_epoch", "final_http_limit_per_loop",
                         "http_limiter_state", "http_counters", "owner_control"})
        self.assertEqual(first["process_id"], os.getpid())
        self.assertEqual(str(uuid.UUID(first["loop_epoch"])), first["loop_epoch"])
        self.assertIsNone(first["final_http_limit_per_loop"])
        self.assertEqual(first["http_limiter_state"], "not_initialized")
        self.assertEqual(first["http_counters"], {"active_requests": 0, "pending_requests": 0})
        self.assertEqual(first["owner_control"], {"foreign_loop_observed": False,
                         "soft_drain_requested": False, "soft_drain_applied": False, "trigger": None})
        first["http_counters"]["active_requests"] = 99
        first["owner_control"]["soft_drain_applied"] = True
        self.assertEqual(self.fx.snapshot()["http_counters"]["active_requests"], 0)
        self.assertFalse(self.fx.snapshot()["owner_control"]["soft_drain_applied"])
        self.assertEqual(self.fx.limiters, {})
        self.assertEqual(self.callbacks, [])

    async def test_same_binding_is_idempotent_but_changed_sha_capacity_callback_reject(self):
        self.fx.bind(self.callback)
        original = self.fx.snapshot()
        self.fx.bind(self.callback)
        self.assertEqual(self.fx.snapshot(), original)
        for values in ({"sha": "sha256:" + "b" * 64}, {"capacity": 3},
                       {"callback": lambda: None}):
            with self.subTest(values=values):
                args = {"callback": self.callback, **values}
                with self.assertRaisesRegex(RuntimeError, "cannot be rebound"):
                    self.fx.bind(**args)
                self.assertEqual(self.fx.snapshot(), original)
        self.assertEqual(self.fx.limiters, {})

    async def test_unbound_explicit_owner_rejects_actual_post_and_snapshot(self):
        transport = Transport()
        with self.assertRaisesRegex(RuntimeError, "not initialized"):
            await self.fx.client(transport).aio_predict(b"image", prompt="unbound")
        with self.assertRaisesRegex(RuntimeError, "no matching serving owner"):
            self.fx.snapshot()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.fx.limiters, {})

    async def test_bound_capacity_domain_rejects_invalid_types_and_limits(self):
        for capacity in (False, True, 0, -1, 129, 2.0, "2"):
            with self.subTest(capacity=capacity):
                with self.assertRaisesRegex(RuntimeError, "binding is invalid"):
                    self.fx.bind(self.callback, capacity=capacity)
                self.assertIsNone(self.fx.owner)
                self.assertEqual(self.fx.limiters, {})
        with self.assertRaisesRegex(RuntimeError, "binding is invalid"):
            self.fx.bind(None)
        self.fx.bind(self.callback, capacity=128)
        self.assertIsNone(self.fx.snapshot()["final_http_limit_per_loop"])

    async def test_three_real_post_clients_share_two_credits_and_literal_results(self):
        self.fx.bind(self.callback)
        transports = [Transport(held=True) for _ in range(3)]
        clients = [self.fx.client(transport) for transport in transports]
        tasks = [asyncio.create_task(client.aio_predict(b"image", prompt=str(i)))
                 for i, client in enumerate(clients)]
        try:
            await asyncio.wait_for(asyncio.gather(*(t.entered.wait() for t in transports[:2])), 3)
            await loop_fence()
            self.assertFalse(transports[2].entered.is_set())
            self.assertEqual(len(self.fx.limiters), 1)
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 2, "pending_requests": 1})
            self.assertEqual(self.fx.snapshot()["final_http_limit_per_loop"], 2)
            transports[0].release.set()
            await asyncio.wait_for(transports[2].entered.wait(), 3)
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 2, "pending_requests": 0})
            for transport in transports:
                transport.release.set()
            self.assertEqual(await asyncio.wait_for(asyncio.gather(*tasks), 3),
                             ["owner-post-result"] * 3)
            for i, transport in enumerate(transports):
                self.assertEqual(transport.calls, [("http://literal.invalid/v1/chat/completions",
                                                   {"literal_request": str(i)})])
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 0, "pending_requests": 0})
        finally:
            for transport in transports:
                transport.release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)

    async def test_foreign_loop_rejects_without_credit_and_requests_once_before_applied(self):
        self.fx.bind(self.callback)
        loop = asyncio.get_running_loop()
        real_notify = loop.call_soon_threadsafe

        async def requests():
            errors = []
            for _ in range(3):
                try:
                    self.fx.limiter()
                except RuntimeError as exc:
                    errors.append(str(exc))
            return errors

        with mock.patch.object(loop, "call_soon_threadsafe", side_effect=real_notify) as notified:
            result = foreign_call(requests)
            self.assertIsNone(result.error)
            self.assertEqual(result.value, ["final POST rejected outside the capacity serving loop"] * 3)
            self.assertNotEqual(result.thread_id, threading.get_ident())
            self.assertEqual(notified.call_count, 1)
            self.assertEqual(self.fx.snapshot()["owner_control"], {
                "foreign_loop_observed": True, "soft_drain_requested": True,
                "soft_drain_applied": False, "trigger": "foreign_event_loop"})
            self.assertEqual(self.callbacks, [])
            self.assertEqual(self.fx.limiters, {})
            await loop_fence()
        self.assertEqual(self.callbacks, [threading.get_ident()])
        self.assertTrue(self.fx.snapshot()["owner_control"]["soft_drain_applied"])
        self.assertEqual(self.fx.snapshot()["http_counters"], {"active_requests": 0, "pending_requests": 0})

    async def test_foreign_post_is_rejected_before_failing_or_awaiting_client_acquisition(self):
        self.fx.bind(self.callback)
        reached = []
        marker = RuntimeError("external client acquisition failure")

        async def requests():
            errors = []
            for kind in ("failure", "await"):
                client = self.fx.client(Transport())

                async def acquire(kind=kind):
                    reached.append(kind)
                    if kind == "failure":
                        raise marker
                    await asyncio.Future()

                client._aio_client = acquire
                try:
                    await asyncio.wait_for(client.aio_predict(b"image", prompt="foreign"), .2)
                except BaseException as exc:
                    errors.append(exc)
            return errors

        result = foreign_call(requests)
        self.assertIsNone(result.error)
        self.assertEqual(reached, [])
        self.assertEqual([str(exc) for exc in result.value],
                         ["final POST rejected outside the capacity serving loop"] * 2)
        self.assertEqual(self.fx.limiters, {})
        self.assertTrue(self.fx.snapshot()["owner_control"]["soft_drain_requested"])
        self.assertFalse(self.fx.snapshot()["owner_control"]["soft_drain_applied"])
        await loop_fence()
        self.assertEqual(self.callbacks, [threading.get_ident()])

    async def test_foreign_fault_preserves_active_owner_and_later_owner_post_drain(self):
        self.fx.bind(self.callback, capacity=1)
        first, later = Transport(held=True), Transport()
        active = asyncio.create_task(self.fx.client(first, 1).aio_predict(b"image", prompt="first"))
        try:
            await asyncio.wait_for(first.entered.wait(), 3)

            async def wrong():
                await self.fx.client(Transport(), 1).aio_predict(b"image", prompt="wrong")

            wrong_result = foreign_call(wrong)
            self.assertIsInstance(wrong_result.error, RuntimeError)
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 1, "pending_requests": 0})
            self.assertEqual(len(self.fx.limiters), 1)
            await loop_fence()
            self.assertEqual(self.callbacks, [threading.get_ident()])
            first.release.set()
            self.assertEqual(await asyncio.wait_for(active, 3), "owner-post-result")
            self.assertEqual(await self.fx.client(later, 1).aio_predict(b"image", prompt="later"),
                             "owner-post-result")
            self.assertEqual(len(later.calls), 1)
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 0, "pending_requests": 0})
            with self.assertRaisesRegex(RuntimeError, "cannot be rebound"):
                self.fx.bind(self.callback, capacity=1)
        finally:
            first.release.set()
            await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 3)

    async def test_matching_loop_h_conflict_rejects_without_creating_or_changing_credits(self):
        self.fx.bind(self.callback)
        for initialized in (False, True):
            if initialized:
                original = self.fx.limiter()
            for capacity in (True, 2.0, 1, 3):
                with self.subTest(initialized=initialized, capacity=capacity):
                    with self.assertRaisesRegex(RuntimeError, "differs from the serving config"):
                        self.fx.limiter(capacity)
                    self.assertEqual(len(self.fx.limiters), int(initialized))
            if initialized:
                self.assertIs(self.fx.limiter(), original)
        self.assertEqual(self.callbacks, [])

    async def test_existing_legacy_limiter_cannot_be_adopted_as_explicit_owner(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            original = self.fx.limiter()
        with self.assertRaisesRegex(RuntimeError, "preceded capacity owner startup"):
            self.fx.bind(self.callback)
        self.assertIsNone(self.fx.owner)
        self.assertEqual(list(self.fx.limiters.values()), [original])

    async def test_forked_pid_and_wrong_config_observation_reject_without_reset(self):
        self.fx.bind(self.callback)
        original = self.fx.snapshot()
        with self.assertRaisesRegex(RuntimeError, "no matching serving owner"):
            self.fx.snapshot("sha256:" + "c" * 64)
        pid = os.getpid()
        with mock.patch.object(self.fx.namespace["_agent_request_os"], "getpid", return_value=pid + 1):
            with self.assertRaisesRegex(RuntimeError, "process changed"):
                self.fx.limiter()
            with self.assertRaisesRegex(RuntimeError, "no matching serving owner"):
                self.fx.snapshot()
            with self.assertRaisesRegex(RuntimeError, "cannot be rebound"):
                self.fx.bind(self.callback)
        self.assertEqual(self.fx.snapshot(), original)
        self.assertEqual(self.fx.limiters, {})

    async def test_foreign_rebind_snapshot_and_direct_callback_cannot_take_owner(self):
        self.fx.bind(self.callback)
        original = self.fx.snapshot()

        async def wrong():
            errors = []
            for operation in (lambda: self.fx.bind(self.callback), self.fx.snapshot,
                              lambda: self.fx.namespace["_apply_capacity_soft_drain"](self.fx.owner)):
                try:
                    operation()
                except RuntimeError as exc:
                    errors.append(str(exc))
            return errors

        result = foreign_call(wrong)
        self.assertIsNone(result.error)
        self.assertEqual(result.value, ["capacity owner cannot be rebound within one process",
                         "capacity HTTP observation has no matching serving owner",
                         "capacity drain callback is outside the serving loop"])
        self.assertEqual(self.fx.snapshot(), original)
        self.assertEqual(self.callbacks, [])

    async def test_callback_exception_preserves_original_error_and_does_not_claim_applied(self):
        marker = RuntimeError("literal soft-drain callback failure")

        def fail():
            self.callbacks.append(threading.get_ident())
            raise marker

        self.fx.bind(fail)
        loop = asyncio.get_running_loop()
        errors = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda unused_loop, context: errors.append(context["exception"]))
        try:
            async def wrong():
                self.fx.limiter()

            result = foreign_call(wrong)
            self.assertIsInstance(result.error, RuntimeError)
            await loop_fence()
            self.assertEqual(errors, [marker])
            self.assertEqual(self.callbacks, [threading.get_ident()])
            self.assertTrue(self.fx.snapshot()["owner_control"]["soft_drain_requested"])
            self.assertFalse(self.fx.snapshot()["owner_control"]["soft_drain_applied"])
        finally:
            loop.set_exception_handler(previous)

    async def test_bound_transport_exception_and_waiter_cancel_return_exact_credits(self):
        self.fx.bind(self.callback, capacity=1)
        marker = RuntimeError("literal transport failure")
        active_transport, waiting_transport = Transport(held=True, failure=marker), Transport()
        active = asyncio.create_task(self.fx.client(active_transport, 1).aio_predict(b"image"))
        waiting = None
        try:
            await asyncio.wait_for(active_transport.entered.wait(), 3)
            waiting = asyncio.create_task(self.fx.client(waiting_transport, 1).aio_predict(b"image"))
            await loop_fence()
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 1, "pending_requests": 1})
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(waiting_transport.calls, [])
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 1, "pending_requests": 0})
            active_transport.release.set()
            with self.assertRaises(RuntimeError) as caught:
                await active
            self.assertIs(caught.exception, marker)
            self.assertEqual(self.fx.snapshot()["http_counters"],
                             {"active_requests": 0, "pending_requests": 0})
        finally:
            active_transport.release.set()
            await asyncio.wait_for(asyncio.gather(active, *([] if waiting is None else [waiting]),
                                                  return_exceptions=True), 3)


class ClosedOwnerLoopTests(unittest.TestCase):
    def test_closed_serving_loop_notification_failure_stays_requested_not_applied(self):
        fx = HttpFixture(generated_http())
        calls = []
        def callback():
            calls.append("applied")
        owner_loop = asyncio.new_event_loop()

        async def bind():
            fx.bind(callback)

        try:
            owner_loop.run_until_complete(bind())
        finally:
            owner_loop.close()

        async def wrong():
            fx.limiter()

        result = foreign_call(wrong)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertIn("closed", str(result.error).lower())
        self.assertEqual(fx.limiters, {})
        self.assertEqual(calls, [])
        self.assertTrue(fx.owner["foreign_loop_observed"])
        self.assertTrue(fx.owner["soft_drain_requested"])
        self.assertFalse(fx.owner["soft_drain_applied"])
        again = foreign_call(wrong)
        self.assertEqual(str(again.error), "final POST rejected outside the capacity serving loop")
        self.assertEqual(calls, [])
        self.assertEqual(fx.limiters, {})
