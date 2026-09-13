"""Independent controller orchestration tests, with no live runtime authority."""

from dataclasses import replace
from threading import Event, get_ident
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.services import m6_service_controller as controller
from disclosure_anchor.application.services.m6_service_controller import (
    ServiceControllerFailure,
    ServiceWork,
    run_service_controller,
)
from tests._m6_service_controller_fixture import (
    DIMENSIONS_EXCEPT_DOCUMENTS,
    Ledger,
    TrackingPool,
    await_event,
    completion,
    original_errors,
    running,
    work,
)


class ServiceControllerTests(unittest.TestCase):
    def arguments(self, **changes):
        values = {
            "work": (work("a"),), "max_in_flight": 1,
            "credits_limit": ResourceCreditVector(documents=1),
            "execute": lambda item, guard: completion(item.attempt_id),
            "stop_requested": lambda: False, "before_submit": lambda: None,
            "on_completion": lambda result: None,
        }
        values.update(changes)
        return values

    def assert_failure(self, call):
        call.finish()
        self.assertIsInstance(call.error, ServiceControllerFailure)
        self.assertEqual(call.error.result.terminal, "failed")
        return call.error

    def test_value_contracts_reject_invalid_identity_or_unproved_disposal(self):
        for attempt in ("", "a b", "a\n", "a\x7f", "a" * 129, 4, True):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                work(attempt)
        for reserve, retained in (
            (ResourceCreditVector(), ResourceCreditVector()),
            (ResourceCreditVector(documents=2), ResourceCreditVector()),
            (ResourceCreditVector(documents=1), ResourceCreditVector(documents=1)),
            (ResourceCreditVector(documents=1, temp_disk_bytes=1),
             ResourceCreditVector(temp_disk_bytes=2)),
            (object(), ResourceCreditVector()),
        ):
            with self.subTest(reserve=reserve, retained=retained), self.assertRaises(ValueError):
                ServiceWork("a", reserve, retained)
        for change in ({"outcome": "pending"}, {"disposal_receipt_sha256": "d" * 64},
                       {"disposal_receipt_sha256": "sha256:" + "D" * 64},
                       {"attempt_id": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(completion("a"), **change)
        self.assertEqual(work("opaque-汉字" + "x" * 119).reservation.documents, 1)
        self.assertEqual(completion("a", "failed").outcome, "failed")

    def test_invalid_controller_inputs_fail_before_executor_or_callbacks(self):
        invalid = [
            {"work": ()}, {"work": []}, {"work": (object(),)},
            {"work": (work("a"), work("a"))},
            {"work": tuple(work(str(i)) for i in range(10_001))},
            {"max_in_flight": True}, {"max_in_flight": 0}, {"max_in_flight": 65},
            {"max_in_flight": 1.0}, {"credits_limit": object()},
            {"credits_limit": ResourceCreditVector()},
            *({"poll_seconds": value} for value in
              (False, 0, -0.1, 1.01, float("nan"), float("inf"), "0.1")),
        ]
        for change in invalid:
            with self.subTest(change=tuple(change)), patch.object(
                controller, "ThreadPoolExecutor"
            ) as pool:
                execute = Mock()
                with self.assertRaises(ValueError):
                    run_service_controller(**self.arguments(execute=execute, **change))
                pool.assert_not_called()
                execute.assert_not_called()

    def test_default_poll_single_disposed_completion_and_callback_thread(self):
        thread = get_ident()
        observations = []

        def execute(item, guard):
            observations.append(("execute", get_ident()))
            guard()
            return completion(item.attempt_id)

        result = run_service_controller(**self.arguments(
            work=(work("a", temp_disk_bytes=9, retained=3),),
            credits_limit=ResourceCreditVector(documents=1, temp_disk_bytes=9),
            execute=execute,
            before_submit=lambda: observations.append(("guard", get_ident())),
            on_completion=lambda value: observations.append(("observed", get_ident())),
        ))
        self.assertEqual(result.terminal, "exhausted")
        self.assertEqual(result.completed, (completion("a"),))
        self.assertEqual((result.not_started, result.unresolved), ((), ()))
        self.assertEqual(result.retained_credits, ResourceCreditVector(temp_disk_bytes=3))
        self.assertEqual([row[0] for row in observations], ["execute", "guard", "observed"])
        self.assertEqual(observations[0][1], observations[1][1])
        self.assertNotEqual(observations[0][1], thread)
        self.assertEqual(observations[2][1], thread)

    def test_refills_completed_slot_while_peer_runs_without_executor_backlog(self):
        ledger = Ledger()
        releases = {name: Event() for name in "abc"}
        first_wait = Event()
        actual_wait = controller.wait

        def observed_wait(futures, **kwargs):
            ledger.add("waiting", len(futures))
            first_wait.set()
            return actual_wait(futures, **kwargs)

        def execute(item, guard):
            guard()
            ledger.add("start", item.attempt_id)
            await_event(releases[item.attempt_id])
            return completion(item.attempt_id)

        with patch.object(controller, "wait", side_effect=observed_wait), patch.object(
            controller, "ThreadPoolExecutor", side_effect=lambda **kw:
                          TrackingPool(ledger=ledger, **kw)), running(
            release=tuple(releases.values()), **self.arguments(
                work=tuple(work(name) for name in "abc"), max_in_flight=2,
                credits_limit=ResourceCreditVector(documents=3), execute=execute)
        ) as call:
            ledger.until(lambda rows: ("start", "a") in rows and ("start", "b") in rows)
            await_event(first_wait)
            self.assertIn(("waiting", 2), ledger.rows)
            self.assertEqual([r for r in ledger.rows if r[0] == "submit"],
                             [("submit", "a"), ("submit", "b")])
            releases["b"].set()
            ledger.until(lambda rows: ("start", "c") in rows)
            self.assertFalse(releases["a"].is_set())
            self.assertFalse(call.done.is_set())
            releases["c"].set()
            releases["a"].set()
            call.finish()
            self.assertIsNone(call.error)
            self.assertEqual(call.value.completed, tuple(completion(n) for n in "abc"))
            self.assertEqual(call.value.retained_credits, ResourceCreditVector())
        self.assertIn(("shutdown", True, False), ledger.rows)

    def test_oldest_fitting_applies_every_non_document_credit_dimension(self):
        for dimension in DIMENSIONS_EXCEPT_DOCUMENTS:
            with self.subTest(dimension=dimension):
                ledger = Ledger()
                releases = {name: Event() for name in "abc"}

                def execute(item, guard):
                    ledger.add("start", item.attempt_id)
                    await_event(releases[item.attempt_id])
                    return completion(item.attempt_id)

                # a(2) + c(1) fits3. Older b(2) cannot fit while a owns2.
                items = (work("a", **{dimension: 2}), work("b", **{dimension: 2}),
                         work("c", **{dimension: 1}))
                with running(release=tuple(releases.values()), **self.arguments(
                    work=items, max_in_flight=3,
                    credits_limit=ResourceCreditVector(documents=3, **{dimension: 3}),
                    execute=execute,
                    on_dispatch=lambda item: ledger.add("dispatch", item.attempt_id),
                )) as call:
                    ledger.until(lambda rows: ("start", "a") in rows and ("start", "c") in rows)
                    self.assertNotIn(("start", "b"), ledger.rows)
                    releases["a"].set()
                    ledger.until(lambda rows: ("start", "b") in rows)
                    self.assertEqual([r[1] for r in ledger.rows if r[0] == "dispatch"],
                                     ["a", "c", "b"])
                    releases["b"].set()
                    releases["c"].set()
                    call.finish()
                    self.assertIsNone(call.error)

    def test_document_credits_limit_actual_execution_below_thread_capacity(self):
        ledger = Ledger()
        first = Event()
        first_wait = Event()
        actual_wait = controller.wait

        def observed_wait(futures, **kwargs):
            ledger.add("waiting", len(futures))
            first_wait.set()
            return actual_wait(futures, **kwargs)

        def execute(item, guard):
            ledger.add("start", item.attempt_id)
            if item.attempt_id == "a":
                await_event(first)
            return completion(item.attempt_id)

        with patch.object(controller, "wait", side_effect=observed_wait), running(
            release=(first,), **self.arguments(
            work=(work("a"), work("b")), max_in_flight=2, execute=execute,
            credits_limit=ResourceCreditVector(documents=1),
        )) as call:
            ledger.until(lambda rows: ("start", "a") in rows)
            await_event(first_wait)
            self.assertIn(("waiting", 1), ledger.rows)
            self.assertEqual([r for r in ledger.rows if r[0] == "start"], [("start", "a")])
            first.set()
            call.finish()
            self.assertIsNone(call.error)
            self.assertEqual([r for r in ledger.rows if r[0] == "start"],
                             [("start", "a"), ("start", "b")])

    def test_disposed_failed_work_releases_transient_but_retained_budget_accumulates(self):
        dispatched = []

        def execute(item, guard):
            dispatched.append(item.attempt_id)
            return completion(item.attempt_id, "failed" if item.attempt_id == "a" else "completed")

        # a reserves6 then retains2; b reserves6 then retains1. With limit8,
        # b fits exactly after a; c(6) cannot fit alongside the retained3.
        items = (work("a", temp_disk_bytes=6, retained=2),
                 work("b", temp_disk_bytes=6, retained=1), work("c", temp_disk_bytes=6))
        with self.assertRaises(ServiceControllerFailure) as caught:
            run_service_controller(**self.arguments(
                work=items, credits_limit=ResourceCreditVector(documents=1, temp_disk_bytes=8),
                execute=execute,
            ))
        result = caught.exception.result
        self.assertEqual(dispatched, ["a", "b"])
        self.assertEqual(result.completed, (completion("a", "failed"), completion("b")))
        self.assertEqual((result.not_started, result.unresolved), (("c",), ()))
        self.assertEqual(result.retained_credits, ResourceCreditVector(temp_disk_bytes=3))
        self.assertIn("retained", str(original_errors(caught.exception.__cause__)[0]))

    def test_worker_failure_stops_supply_and_drains_live_peer_with_original_cause(self):
        failure = RuntimeError("original transport outcome unknown")
        ledger = Ledger()
        fail = Event()
        peer = Event()
        draining = Event()
        actual_wait = controller.wait

        def observed_wait(futures, **kwargs):
            if fail.is_set() and len(futures) == 1:
                draining.set()
            return actual_wait(futures, **kwargs)

        def execute(item, guard):
            ledger.add("start", item.attempt_id)
            await_event(fail if item.attempt_id == "a" else peer)
            if item.attempt_id == "a":
                raise failure
            return completion(item.attempt_id)

        items = (work("a", temp_disk_bytes=7), work("b", temp_disk_bytes=5, retained=2),
                 work("c", temp_disk_bytes=1))
        with patch.object(controller, "wait", side_effect=observed_wait), running(
            release=(fail, peer), **self.arguments(work=items, max_in_flight=2,
                credits_limit=ResourceCreditVector(documents=2, temp_disk_bytes=12), execute=execute)
        ) as call:
            ledger.until(lambda rows: len(rows) == 2)
            fail.set()
            await_event(draining)
            self.assertFalse(call.done.is_set())
            self.assertNotIn(("start", "c"), ledger.rows)
            peer.set()
            error = self.assert_failure(call)
            self.assertIn(failure, original_errors(error.__cause__))
            self.assertEqual(error.result.completed, (completion("b"),))
            self.assertEqual((error.result.not_started, error.result.unresolved), (("c",), ("a",)))
            self.assertEqual(error.result.retained_credits,
                             ResourceCreditVector(documents=1, temp_disk_bytes=9))

    def test_two_original_worker_failures_are_both_preserved(self):
        ledger = Ledger()
        release = Event()
        errors = {"a": RuntimeError("a-original"), "b": OSError("b-original")}

        def execute(item, guard):
            ledger.add("start", item.attempt_id)
            await_event(release)
            raise errors[item.attempt_id]

        with running(release=(release,), **self.arguments(
            work=tuple(work(n) for n in "abc"), max_in_flight=2,
            credits_limit=ResourceCreditVector(documents=2), execute=execute,
        )) as call:
            ledger.until(lambda rows: len(rows) == 2)
            release.set()
            error = self.assert_failure(call)
            self.assertEqual(set(original_errors(error.__cause__)), set(errors.values()))
            self.assertEqual(error.result.unresolved, ("a", "b"))
            self.assertEqual(error.result.not_started, ("c",))
            self.assertEqual(error.result.retained_credits, ResourceCreditVector(documents=2))

    def test_wrong_completion_type_or_attempt_cannot_release_reservation(self):
        for returned in (object(), completion("wrong")):
            with self.subTest(returned=returned), self.assertRaises(ServiceControllerFailure) as caught:
                run_service_controller(**self.arguments(
                    work=(work("a", temp_disk_bytes=7, retained=1), work("b")),
                    credits_limit=ResourceCreditVector(documents=1, temp_disk_bytes=7),
                    execute=lambda item, guard: returned,
                ))
            result = caught.exception.result
            self.assertEqual((result.completed, result.not_started, result.unresolved),
                             ((), ("b",), ("a",)))
            self.assertEqual(result.retained_credits,
                             ResourceCreditVector(documents=1, temp_disk_bytes=7))
            self.assertIsInstance(original_errors(caught.exception.__cause__)[0], ValueError)

    def test_completion_observer_failure_keeps_disposal_truth_and_drains_peer(self):
        ledger = Ledger()
        a = Event()
        b = Event()
        observed = Event()
        failure = OSError("completion journal append unknown")

        def execute(item, guard):
            ledger.add("start", item.attempt_id)
            await_event(a if item.attempt_id == "a" else b)
            return completion(item.attempt_id)

        def observe(result):
            ledger.add("observe", result.attempt_id)
            if result.attempt_id == "a":
                observed.set()
                raise failure

        items = (work("a", temp_disk_bytes=7, retained=1),
                 work("b", temp_disk_bytes=5, retained=2), work("c"))
        with running(release=(a, b), **self.arguments(
            work=items, max_in_flight=2, execute=execute, on_completion=observe,
            credits_limit=ResourceCreditVector(documents=2, temp_disk_bytes=12),
        )) as call:
            ledger.until(lambda rows: ("start", "a") in rows and ("start", "b") in rows)
            a.set()
            await_event(observed)
            self.assertFalse(call.done.is_set())
            b.set()
            error = self.assert_failure(call)
            self.assertEqual(error.result.completed, (completion("a"), completion("b")))
            self.assertEqual((error.result.not_started, error.result.unresolved), (("c",), ()))
            self.assertEqual(error.result.retained_credits, ResourceCreditVector(temp_disk_bytes=3))
            self.assertIn(failure, original_errors(error.__cause__))
            self.assertEqual([r for r in ledger.rows if r[0] == "observe"],
                             [("observe", "a"), ("observe", "b")])

    def test_initial_stop_admits_nothing_and_does_not_call_dispatch_or_submit_guard(self):
        execute, dispatch, guard = Mock(), Mock(), Mock()
        result = run_service_controller(**self.arguments(
            work=(work("a"), work("b")), stop_requested=lambda: True,
            execute=execute, on_dispatch=dispatch, before_submit=guard,
        ))
        self.assertEqual((result.terminal, result.completed, result.not_started, result.unresolved),
                         ("stopped", (), ("a", "b"), ()))
        self.assertEqual(result.retained_credits, ResourceCreditVector())
        execute.assert_not_called()
        dispatch.assert_not_called()
        guard.assert_not_called()

    def test_stop_latches_before_late_post_guard_and_drains_owned_attempts(self):
        ledger = Ledger()
        release = Event()
        stop = Event()
        stop_observed = Event()
        stopped_wait = Event()
        actual_wait = controller.wait

        def should_stop():
            if stop.is_set():
                stop_observed.set()
                return True
            return False

        def observed_wait(futures, **kwargs):
            if stop_observed.is_set():
                stopped_wait.set()
            return actual_wait(futures, **kwargs)

        def execute(item, guard):
            if item.attempt_id == "a":
                guard()
                ledger.add("post", "a")
            ledger.add("start", item.attempt_id)
            await_event(release)
            if item.attempt_id == "b":
                try:
                    guard()
                except RuntimeError:
                    ledger.add("guard-refused", "b")
                    return completion("b", "failed")
                ledger.add("post", "b")
            return completion(item.attempt_id)

        checked = Mock()
        with patch.object(controller, "wait", side_effect=observed_wait), running(
            release=(release,), **self.arguments(work=tuple(work(n) for n in "abc"),
                max_in_flight=2, credits_limit=ResourceCreditVector(documents=2),
                execute=execute, stop_requested=should_stop, before_submit=checked)
        ) as call:
            ledger.until(lambda rows: ("start", "a") in rows and ("start", "b") in rows)
            stop.set()
            await_event(stopped_wait)
            self.assertFalse(call.done.is_set())
            release.set()
            call.finish()
            self.assertIsNone(call.error)
            self.assertEqual(call.value.terminal, "stopped")
            self.assertEqual(call.value.completed, (completion("a"), completion("b", "failed")))
            self.assertEqual(call.value.not_started, ("c",))
            self.assertEqual(call.value.unresolved, ())
            self.assertEqual([r for r in ledger.rows if r[0] == "post"], [("post", "a")])
            self.assertIn(("guard-refused", "b"), ledger.rows)
            checked.assert_called_once_with()

    def test_actual_submission_guard_error_is_original_and_never_posts(self):
        failure = OSError("grant unavailable at POST")
        posts = Mock()

        def execute(item, guard):
            guard()
            posts(item.attempt_id)
            return completion(item.attempt_id)

        with self.assertRaises(ServiceControllerFailure) as caught:
            run_service_controller(**self.arguments(
                work=(work("a"), work("b")), execute=execute,
                before_submit=Mock(side_effect=failure),
            ))
        posts.assert_not_called()
        self.assertIn(failure, original_errors(caught.exception.__cause__))
        self.assertEqual(caught.exception.result.unresolved, ("a",))
        self.assertEqual(caught.exception.result.not_started, ("b",))

    def test_guard_rechecks_failure_latch_after_external_grant_callback(self):
        callback_entered = Event()
        callback_release = Event()
        peer_started = Event()
        peer_fail = Event()
        draining = Event()
        actual_wait = controller.wait
        failure = RuntimeError("peer failed while grant was checked")
        posts = Mock()

        def checked():
            callback_entered.set()
            await_event(callback_release)

        def observed_wait(futures, **kwargs):
            if peer_fail.is_set() and len(futures) == 1:
                draining.set()
            return actual_wait(futures, **kwargs)

        def execute(item, guard):
            if item.attempt_id == "a":
                guard()
                posts("a")
                return completion("a")
            peer_started.set()
            await_event(peer_fail)
            raise failure

        with patch.object(controller, "wait", side_effect=observed_wait), running(
            release=(callback_release, peer_fail), **self.arguments(
                work=(work("a"), work("b")), max_in_flight=2,
                credits_limit=ResourceCreditVector(documents=2), execute=execute,
                before_submit=checked)
        ) as call:
            await_event(callback_entered)
            await_event(peer_started)
            peer_fail.set()
            await_event(draining)
            callback_release.set()
            error = self.assert_failure(call)
            posts.assert_not_called()
            failures = original_errors(error.__cause__)
            self.assertIn(failure, failures)
            self.assertEqual(len(failures), 2)
            self.assertEqual(error.result.unresolved, ("a", "b"))
            self.assertEqual(error.result.retained_credits, ResourceCreditVector(documents=2))

    def test_dispatch_is_on_controller_thread_and_precedes_submit_then_worker_guard(self):
        ledger = Ledger()
        controller_thread = get_ident()

        def dispatch(item):
            ledger.add("dispatch", item.attempt_id, get_ident())

        def execute(item, guard):
            ledger.add("execute", item.attempt_id, get_ident())
            guard()
            ledger.add("post", item.attempt_id)
            return completion(item.attempt_id)

        with patch.object(controller, "ThreadPoolExecutor", side_effect=lambda **kw:
                          TrackingPool(ledger=ledger, **kw)):
            result = run_service_controller(**self.arguments(
                execute=execute, on_dispatch=dispatch,
                before_submit=lambda: ledger.add("guard", get_ident()),
            ))
        self.assertEqual([r[0] for r in ledger.rows],
                         ["dispatch", "submit", "execute", "guard", "post", "shutdown"])
        self.assertEqual(ledger.rows[0], ("dispatch", "a", controller_thread))
        self.assertNotEqual(ledger.rows[2][2], controller_thread)
        self.assertEqual(ledger.rows[2][2], ledger.rows[3][1])
        self.assertEqual(result.completed, (completion("a"),))

    def test_dispatch_callback_failure_retains_full_selected_reservation_and_drains_peer(self):
        ledger = Ledger()
        peer = Event()
        dispatch_failed = Event()
        failure = OSError("durable dispatch intent outcome unknown")

        def dispatch(item):
            ledger.add("dispatch", item.attempt_id)
            if item.attempt_id == "b":
                dispatch_failed.set()
                raise failure

        def execute(item, guard):
            ledger.add("execute", item.attempt_id)
            await_event(peer)
            return completion(item.attempt_id)

        with patch.object(controller, "ThreadPoolExecutor", side_effect=lambda **kw:
                          TrackingPool(ledger=ledger, **kw)), running(
            release=(peer,), **self.arguments(
                work=(work("a", temp_disk_bytes=5, retained=1),
                      work("b", temp_disk_bytes=7, retained=2), work("c")),
                max_in_flight=2, credits_limit=ResourceCreditVector(documents=2, temp_disk_bytes=12),
                execute=execute, on_dispatch=dispatch)
        ) as call:
            await_event(dispatch_failed)
            self.assertFalse(call.done.is_set())
            self.assertEqual([r for r in ledger.rows if r[0] == "submit"], [("submit", "a")])
            peer.set()
            error = self.assert_failure(call)
            self.assertIn(failure, original_errors(error.__cause__))
            self.assertEqual(error.result.completed, (completion("a"),))
            self.assertEqual((error.result.not_started, error.result.unresolved), (("c",), ("b",)))
            self.assertEqual(error.result.retained_credits,
                             ResourceCreditVector(documents=1, temp_disk_bytes=8))

    def test_submit_can_start_then_raise_and_unknown_future_still_drains_without_adoption(self):
        ledger = Ledger()
        a = Event()
        b = Event()
        shutdown = Event()
        failure = RuntimeError("executor failed after actual enqueue")

        def execute(item, guard):
            ledger.add("start", item.attempt_id)
            await_event(a if item.attempt_id == "a" else b)
            ledger.add("returned", item.attempt_id)
            return completion(item.attempt_id)

        with patch.object(controller, "ThreadPoolExecutor", side_effect=lambda **kw:
                          TrackingPool(ledger=ledger, raise_after_submit=("b", failure),
                                       shutdown_entered=shutdown, **kw)), running(
            release=(a, b), **self.arguments(
                work=(work("a", temp_disk_bytes=5, retained=1),
                      work("b", temp_disk_bytes=7, retained=2), work("c")),
                max_in_flight=2, credits_limit=ResourceCreditVector(documents=2, temp_disk_bytes=12),
                execute=execute)
        ) as call:
            ledger.until(lambda rows: ("start", "a") in rows and ("start", "b") in rows)
            a.set()
            await_event(shutdown)
            self.assertFalse(call.done.is_set(), "shutdown must wait for actually enqueued b")
            self.assertNotIn(("start", "c"), ledger.rows)
            b.set()
            error = self.assert_failure(call)
            self.assertIn(("returned", "b"), ledger.rows)
            self.assertIn(failure, original_errors(error.__cause__))
            self.assertEqual(error.result.completed, (completion("a"),))
            self.assertEqual((error.result.not_started, error.result.unresolved), (("c",), ("b",)))
            self.assertEqual(error.result.retained_credits,
                             ResourceCreditVector(documents=1, temp_disk_bytes=8))

    def test_stop_observer_error_or_nonboolean_fails_without_dispatch(self):
        original = OSError("stop observation unavailable")
        for observer in (lambda: 1, Mock(side_effect=original)):
            with self.subTest(observer=observer):
                execute = Mock()
                with self.assertRaises(ServiceControllerFailure) as caught:
                    run_service_controller(**self.arguments(execute=execute, stop_requested=observer))
                execute.assert_not_called()
                result = caught.exception.result
                self.assertEqual((result.completed, result.not_started, result.unresolved), ((), ("a",), ()))
                self.assertEqual(result.retained_credits, ResourceCreditVector())
                errors = original_errors(caught.exception.__cause__)
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0] is original or isinstance(errors[0], TypeError))


if __name__ == "__main__":
    unittest.main()
