"""Deterministic probes for the serving-loop lag and collector-pause trace.

Every case drives the real probe on a real asyncio loop and reads the exact
lines it writes.  No serving process, generated API, container or GPU is used;
these are implementation acceptance probes, not independent qualification.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr
from typing import Any
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol


def trace_lines(captured: io.StringIO) -> list[dict[str, Any]]:
    return [
        json.loads(line[len(protocol._LOOP_TRACE_PREFIX):])
        for line in captured.getvalue().splitlines()
        if line.startswith(protocol._LOOP_TRACE_PREFIX)
    ]


class ServingLoopProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"MINERU_PHASE_TRACE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.probe = protocol.ServingLoopProbe()
        self.addCleanup(self.probe.close)
        self.captured = io.StringIO()

    async def asyncSetUp(self) -> None:
        # IsolatedAsyncioTestCase runs its loop in debug mode, whose
        # slow-callback notice ("Executing <Task …> took 0.1 seconds") is
        # logged through the asyncio logger's last-resort handler straight
        # into the redirected stderr whenever the machine is busy. It is not
        # probe output; keep it out of the captured stream so the line-level
        # assertions below only ever see the probe's own trace lines.
        asyncio.get_running_loop().slow_callback_duration = 3600.0

    async def test_a_blocked_loop_reports_its_scheduling_lag(self) -> None:
        with redirect_stderr(self.captured):
            self.probe.start(asyncio.get_running_loop())
            asyncio.get_running_loop().call_soon(time.sleep, 0.25)
            await asyncio.sleep(0.45)
            self.probe.close()
        lag = [event for event in trace_lines(self.captured) if event["event"] == "lag"]
        self.assertTrue(lag)
        self.assertEqual(set(lag[0]), {"schema", "event", "lag_ms", "monotonic_ns"})
        self.assertEqual(lag[0]["schema"], "mineru-loop-trace.v1")
        self.assertGreaterEqual(lag[0]["lag_ms"], 100)
        self.assertGreater(lag[0]["monotonic_ns"], 0)

    async def test_a_forced_collection_reports_its_pause(self) -> None:
        with redirect_stderr(self.captured), patch.object(
            protocol, "_LOOP_TRACE_GC_PAUSE_NS", 0
        ):
            self.probe.start(asyncio.get_running_loop())
            gc.collect(2)
            self.probe.close()
        collections = [
            event
            for event in trace_lines(self.captured)
            if event["event"] == "gc" and event["generation"] == 2
        ]
        self.assertEqual(len(collections), 1)
        self.assertEqual(
            set(collections[0]),
            {"schema", "event", "generation", "pause_ms", "collected", "monotonic_ns"},
        )
        self.assertGreaterEqual(collections[0]["pause_ms"], 0)
        self.assertGreaterEqual(collections[0]["collected"], 0)

    async def test_the_summary_reports_window_counters_and_dropped_lines(self) -> None:
        # Collector pauses stay below the (raised) threshold so the dropped
        # count conserves against lag lines alone.
        with redirect_stderr(self.captured), patch.multiple(
            protocol,
            _LOOP_TRACE_TICK_SECONDS=0.001,
            _LOOP_TRACE_LAG_NS=0,
            _LOOP_TRACE_GC_PAUSE_NS=10**12,
            _LOOP_TRACE_SUMMARY_NS=1_000_000_000,
        ):
            self.probe.start(asyncio.get_running_loop())
            await asyncio.sleep(1.2)
            self.probe.close()
        events = trace_lines(self.captured)
        lag = [event for event in events if event["event"] == "lag"]
        summaries = [event for event in events if event["event"] == "summary"]
        self.assertEqual(len(lag), protocol._LOOP_TRACE_RATE_LIMIT)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(
            set(summaries[0]),
            {
                "schema", "event", "window_seconds", "max_lag_ms", "lag_count",
                "gc_max_pause_ms", "gc_count", "dropped", "monotonic_ns",
            },
        )
        self.assertEqual(summaries[0]["window_seconds"], 1)
        self.assertGreater(summaries[0]["dropped"], 0)
        self.assertEqual(
            summaries[0]["lag_count"], summaries[0]["dropped"] + len(lag)
        )
        self.assertGreaterEqual(summaries[0]["max_lag_ms"], 0)
        self.assertGreaterEqual(summaries[0]["gc_count"], 0)
        self.assertGreaterEqual(summaries[0]["gc_max_pause_ms"], 0)

    async def test_a_worker_thread_collection_reports_and_keeps_observing(self) -> None:
        with redirect_stderr(self.captured), patch.object(
            protocol, "_LOOP_TRACE_GC_PAUSE_NS", 0
        ):
            self.probe.start(asyncio.get_running_loop())
            registered = list(gc.callbacks)
            await asyncio.to_thread(gc.collect, 2)
            await asyncio.to_thread(gc.collect, 2)
            still_attached = list(gc.callbacks) == registered
            self.probe.close()
        collections = [
            event
            for event in trace_lines(self.captured)
            if event["event"] == "gc" and event["generation"] == 2
        ]
        self.assertEqual(len(collections), 2)
        self.assertTrue(still_attached)

    async def test_close_from_a_worker_thread_detaches_and_silences(self) -> None:
        registered = list(gc.callbacks)
        with redirect_stderr(self.captured), patch.multiple(
            protocol,
            _LOOP_TRACE_TICK_SECONDS=0.001,
            _LOOP_TRACE_LAG_NS=0,
            _LOOP_TRACE_GC_PAUSE_NS=0,
        ):
            self.probe.start(asyncio.get_running_loop())
            await asyncio.sleep(0.05)
            self.assertNotEqual(list(gc.callbacks), registered)
            emitted = len(trace_lines(self.captured))
            self.assertGreater(emitted, 0)
            await asyncio.to_thread(self.probe.close)
            self.assertEqual(list(gc.callbacks), registered)
            await asyncio.to_thread(gc.collect, 2)
            await asyncio.sleep(0.1)
        self.assertEqual(len(trace_lines(self.captured)), emitted)

    async def test_loop_and_worker_emits_share_one_rate_limited_window(self) -> None:
        # One collection costs whatever this process heap costs, so the
        # worker is bounded by the observation window rather than a count.
        stop = threading.Event()
        self.addCleanup(stop.set)

        def collect_until_stopped() -> None:
            while not stop.is_set():
                gc.collect(2)

        async def close_window() -> None:
            await asyncio.sleep(1.2)
            stop.set()

        with redirect_stderr(self.captured), patch.multiple(
            protocol,
            _LOOP_TRACE_TICK_SECONDS=0.001,
            _LOOP_TRACE_LAG_NS=0,
            _LOOP_TRACE_GC_PAUSE_NS=0,
            _LOOP_TRACE_SUMMARY_NS=1_000_000_000,
        ):
            self.probe.start(asyncio.get_running_loop())
            await asyncio.gather(
                asyncio.to_thread(collect_until_stopped), close_window()
            )
            self.probe.close()
        events = trace_lines(self.captured)
        window: list[dict[str, Any]] = []
        for event in events:
            if event["event"] == "summary":
                break
            window.append(event)
        else:
            self.fail("the observation window never reported a summary")
        summary = events[len(window)]
        observations = [item for item in window if item["event"] in {"lag", "gc"}]
        self.assertEqual(len(observations), len(window))
        self.assertEqual(len(observations), protocol._LOOP_TRACE_RATE_LIMIT)
        self.assertEqual(
            summary["lag_count"] + summary["gc_count"],
            len(observations) + summary["dropped"],
        )
        self.assert_windows_conserve_events(events)

    def assert_windows_conserve_events(self, events: list[dict[str, Any]]) -> None:
        """Every counted observation is either written or reported as dropped."""
        summaries = [event for event in events if event["event"] == "summary"]
        self.assertTrue(summaries)
        written = sum(
            1
            for event in events[: events.index(summaries[-1])]
            if event["event"] in {"lag", "gc"}
        )
        self.assertEqual(
            sum(event["lag_count"] + event["gc_count"] for event in summaries),
            written + sum(event["dropped"] for event in summaries),
        )

    async def test_every_window_conserves_events_under_a_collecting_worker(self) -> None:
        stop = threading.Event()
        self.addCleanup(stop.set)

        def collect_until_stopped() -> None:
            while not stop.is_set():
                gc.collect(2)

        async def close_window() -> None:
            await asyncio.sleep(0.4)
            stop.set()

        with redirect_stderr(self.captured), patch.multiple(
            protocol,
            _LOOP_TRACE_TICK_SECONDS=0.001,
            _LOOP_TRACE_LAG_NS=0,
            _LOOP_TRACE_GC_PAUSE_NS=0,
            _LOOP_TRACE_SUMMARY_NS=0,
        ):
            self.probe.start(asyncio.get_running_loop())
            await asyncio.gather(
                asyncio.to_thread(collect_until_stopped), close_window()
            )
            self.probe.close()
        events = trace_lines(self.captured)
        self.assertGreaterEqual(
            len([event for event in events if event["event"] == "summary"]), 3
        )
        self.assert_windows_conserve_events(events)

    async def test_a_collection_inside_the_write_path_does_not_deadlock(self) -> None:
        # A write that is not itself a collector callback can start one, and
        # that collection re-enters the same write path on the same thread.
        class CollectingStream(io.StringIO):
            nested = False

            def write(self, payload: str) -> int:
                written = super().write(payload)
                if not self.nested:
                    self.nested = True
                    gc.collect(2)
                return written

        captured = CollectingStream()
        with redirect_stderr(captured), patch.multiple(
            protocol, _LOOP_TRACE_GC_PAUSE_NS=0, _LOOP_TRACE_LAG_NS=0
        ):
            self.probe.start(asyncio.get_running_loop())
            worker = threading.Thread(
                target=self.probe._emit,
                args=({"event": "lag", "lag_ms": 0.0}, time.monotonic_ns()),
                kwargs={"droppable": True},
                daemon=True,
            )
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "the trace write path deadlocked")
            self.assertTrue(captured.nested)
            with self.probe._state:
                counted, dropped = self.probe._gc_count, self.probe._dropped
            self.probe.close()
        collections = [
            event for event in trace_lines(captured) if event["event"] == "gc"
        ]
        self.assertGreaterEqual(counted, 1)
        self.assertEqual(counted, len(collections) + dropped)

    async def test_the_output_lock_is_never_taken_while_the_state_lock_is_held(self) -> None:
        # Every write is observed with the state lock's ownership at entry.
        # Collections begin on a worker thread, inside the write path, and
        # inside this thread's own serialization: the last one is the case
        # where a nested emission used to write while the state lock was held.
        state_owned_at_write: list[bool] = []
        original_write = self.probe._write
        original_line = self.probe._line
        collected_in_line: list[int] = []

        def observed_write(line: str) -> None:
            state_owned_at_write.append(self.probe._state._is_owned())
            original_write(line)

        def collecting_line(record: dict[str, Any]) -> str:
            # A lag line is serialized from the tick on the loop thread, never
            # from inside a collection.  The generation counter is recorded so
            # a collection CPython suppressed (another thread's in flight)
            # cannot let the case pass silently.
            if (
                not collected_in_line
                and record.get("event") == "lag"
                and threading.current_thread() is threading.main_thread()
            ):
                before = gc.get_stats()[2]["collections"]
                gc.collect(2)
                collected_in_line.append(gc.get_stats()[2]["collections"] - before)
            return original_line(record)

        class CollectingStream(io.StringIO):
            nested = 0

            def write(self, payload: str) -> int:
                written = super().write(payload)
                if self.nested < 3:
                    self.nested += 1
                    gc.collect(2)
                return written

        captured = CollectingStream()
        self.probe._write = observed_write
        self.probe._line = collecting_line
        with redirect_stderr(captured), patch.multiple(
            protocol,
            _LOOP_TRACE_TICK_SECONDS=0.001,
            _LOOP_TRACE_LAG_NS=0,
            _LOOP_TRACE_GC_PAUSE_NS=0,
        ):
            self.probe.start(asyncio.get_running_loop())
            for _ in range(3):
                await asyncio.to_thread(gc.collect, 2)
                await asyncio.sleep(0.02)
            self.probe.close()
        events = trace_lines(captured)
        self.assertEqual(collected_in_line, [1], "the in-serialization collection never ran")
        self.assertEqual(captured.nested, 3)
        self.assertGreater(len(state_owned_at_write), 0)
        self.assertEqual(state_owned_at_write, [False] * len(state_owned_at_write))
        self.assertIn("gc", {event["event"] for event in events})
        with self.probe._state:
            self.assertEqual(self.probe._emitting, set())
            self.assertIsNone(self.probe._pending_failure)
            self.assertGreaterEqual(self.probe._dropped, 1)
        # No torn or nested write: every trace line starts with exactly one
        # prefix, and nothing that is not a trace line carries a trace fragment.
        for line in captured.getvalue().splitlines():
            if protocol._LOOP_TRACE_PREFIX in line:
                self.assertTrue(line.startswith(protocol._LOOP_TRACE_PREFIX), line)
                self.assertEqual(line.count(protocol._LOOP_TRACE_PREFIX), 1, line)
            else:
                self.assertNotIn('"event"', line)

    async def test_a_failure_inside_the_write_path_is_reported_after_the_line(self) -> None:
        # A collector callback that faults while this thread is writing its own
        # line must not write ahead of or inside that line: the report follows
        # it, whole, and the hook is released from the serving loop.
        registered = list(gc.callbacks)
        original_milliseconds = protocol._trace_milliseconds
        faults: list[str] = []

        def faulting_milliseconds(duration_ns: int) -> float:
            if threading.current_thread().name == "probe-writer" and not faults:
                faults.append("raised")
                raise RuntimeError("controlled collector fault")
            return original_milliseconds(duration_ns)

        class CollectingStream(io.StringIO):
            def write(self, payload: str) -> int:
                # The collection begins as the write starts, before any byte
                # of the line in progress reaches the stream.
                if threading.current_thread().name == "probe-writer" and not faults:
                    gc.collect(2)
                return super().write(payload)

        captured = CollectingStream()
        with redirect_stderr(captured), patch.multiple(
            protocol, _LOOP_TRACE_GC_PAUSE_NS=0, _LOOP_TRACE_LAG_NS=0,
            _trace_milliseconds=faulting_milliseconds,
        ):
            self.probe.start(asyncio.get_running_loop())
            worker = threading.Thread(
                target=self.probe._emit,
                args=({"event": "lag", "lag_ms": 0.0}, time.monotonic_ns()),
                kwargs={"droppable": True},
                daemon=True, name="probe-writer",
            )
            worker.start()
            await asyncio.to_thread(worker.join, 5)
            self.assertFalse(worker.is_alive(), "the failing write path deadlocked")
            await asyncio.sleep(0.05)
        self.assertEqual(faults, ["raised"])
        # Only prefixed lines are trace records; the report follows the line.
        self.assertEqual(
            [event["event"] for event in trace_lines(captured)],
            ["lag", "probe_failed"],
        )
        self.assertEqual(list(gc.callbacks), registered)
        with self.probe._state:
            self.assertTrue(self.probe._stopped)
            self.assertIsNone(self.probe._pending_failure)

    async def test_a_gc_path_fault_releases_the_hook_from_the_loop(self) -> None:
        registered = list(gc.callbacks)
        with redirect_stderr(self.captured), patch.object(
            protocol.ServingLoopProbe, "_emit", autospec=True,
            side_effect=RuntimeError("controlled probe fault"),
        ), patch.object(protocol, "_LOOP_TRACE_GC_PAUSE_NS", 0):
            self.probe.start(asyncio.get_running_loop())
            self.assertNotEqual(list(gc.callbacks), registered)
            await asyncio.to_thread(gc.collect, 2)
            self.assertNotEqual(list(gc.callbacks), registered)
            await asyncio.sleep(0.05)
            self.assertEqual(list(gc.callbacks), registered)
        events = trace_lines(self.captured)
        self.assertEqual([event["event"] for event in events], ["probe_failed"])
        self.assertEqual(events[0]["reason"], "RuntimeError")

    async def test_an_invalid_switch_value_refuses_to_attach(self) -> None:
        registered = list(gc.callbacks)
        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "sometimes"}):
            with self.assertRaisesRegex(RuntimeError, "MINERU_PHASE_TRACE"):
                self.probe.start(asyncio.get_running_loop())
        self.assertEqual(list(gc.callbacks), registered)
        with self.probe._state:
            self.assertIsNone(self.probe._loop)

    async def test_the_probe_is_inert_without_the_phase_trace_switch(self) -> None:
        registered = list(gc.callbacks)
        with patch.dict(os.environ, {"MINERU_PHASE_TRACE": "0"}), redirect_stderr(
            self.captured
        ), patch.object(protocol, "_LOOP_TRACE_LAG_NS", 0):
            self.probe.start(asyncio.get_running_loop())
            await asyncio.sleep(0.25)
        self.assertEqual(list(gc.callbacks), registered)
        self.assertEqual(trace_lines(self.captured), [])
        self.probe.close()
        self.probe.close()
        self.assertEqual(list(gc.callbacks), registered)

    async def test_a_failing_tick_reports_once_and_stops_observing(self) -> None:
        registered = list(gc.callbacks)
        with redirect_stderr(self.captured), patch.object(
            protocol.ServingLoopProbe, "_emit", autospec=True,
            side_effect=RuntimeError("controlled probe fault"),
        ), patch.object(protocol, "_LOOP_TRACE_LAG_NS", 0):
            self.probe.start(asyncio.get_running_loop())
            await asyncio.sleep(0.35)
        events = trace_lines(self.captured)
        self.assertEqual([event["event"] for event in events], ["probe_failed"])
        self.assertEqual(events[0]["reason"], "RuntimeError")
        self.assertEqual(list(gc.callbacks), registered)
