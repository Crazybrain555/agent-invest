"""Bounded owner control without a live Mac kernel, SSH or shared runtime."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import os
import socket
import struct
import threading
import tempfile
import sys
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.dedicated_mac_observer import (
    DedicatedMacObserver, DedicatedMacObserverRequest, _observer_child, _read_identity, _watch_owner,
)
from disclosure_anchor.adapters.runtime.mac_observer_identity import MacObserverIdentityReader
from disclosure_anchor.application.contracts.resident_session_evidence import (
    canonical_bytes, check_mac_observer_identity,
)
from tests.unit.test_synchronized_telemetry_observer import (
    _collector_spec, _Sampler, _gpu_snapshot, _host_snapshot,
)
from tests.unit.test_synchronized_telemetry_observer_v3 import _api_profile


def _identity_bytes():
    boot = "12345678-1234-4234-8234-1234567890AB"
    return canonical_bytes({
        "contract_version": "mineru.mac-observer-identity.v1",
        "process": {"pid": 4321, "parent_pid": 1234, "uid": 501,
                    "start_time_unix_seconds": 1788765035, "start_time_microseconds": 564385,
                    "boot_session_uuid": boot},
        "clock": {"boot_session_uuid": boot, "kernel_release": "25.6.0",
                  "implementation": "mach_absolute_time()", "monotonic": True,
                  "adjustable": False, "resolution_seconds": 4.166666666666666e-08},
    })


def _request():
    clock = check_mac_observer_identity(_identity_bytes()).clock_domain_identity_sha256
    return DedicatedMacObserverRequest(
        Path("/synthetic/not-opened"), _api_profile(),
        _collector_spec(lane="gpu", observer_clock_domain_identity_sha256=clock),
        _collector_spec(lane="host", observer_clock_domain_identity_sha256=clock),
        0.3, "12345678-1234-4234-8234-1234567890ab",
    )


def _clocked_test_collector_factory(config):
    """Synthetic GPU/API values, with the actual observer's Mac clock only."""
    snapshot = _gpu_snapshot() if config["lane"] == "gpu" else _host_snapshot()
    snapshot = replace(snapshot, identity=replace(snapshot.identity, clock_domain_identity_sha256=config["observer_clock_domain_identity_sha256"]))
    return _Sampler(snapshot)


class DedicatedMacObserverTests(unittest.TestCase):
    def _child(self, child, request, errors, reader=lambda: _identity_bytes()):
        try:
            _observer_child(child, request, reader)
        except BaseException as exc:
            errors.append(exc)

    def test_identity_go_and_final_identity_bracket_the_only_runner_call(self):
        parent, child = socket.socketpair()
        errors = []
        try:
            with patch("disclosure_anchor.adapters.runtime.dedicated_mac_observer.run_synchronized_telemetry_observer") as run:
                worker = threading.Thread(target=self._child, args=(child, _request(), errors))
                worker.start()
                self.assertEqual(_read_identity(parent, timeout=1), _identity_bytes())
                run.assert_not_called()
                parent.sendall(b"GO")
                self.assertEqual(_read_identity(parent, timeout=2), _identity_bytes())
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                run.assert_called_once()
                self.assertEqual(run.call_args.kwargs["observer_identity"], check_mac_observer_identity(_identity_bytes()))
        finally:
            parent.close()
            child.close()


    def test_lost_owner_before_go_never_creates_collectors_or_artifacts(self):
        parent, child = socket.socketpair()
        errors = []
        with patch("disclosure_anchor.adapters.runtime.dedicated_mac_observer.run_synchronized_telemetry_observer") as run:
            worker = threading.Thread(target=self._child, args=(child, _request(), errors))
            worker.start()
            _read_identity(parent, timeout=1)
            parent.close()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], EOFError)
            run.assert_not_called()

    def test_partial_and_oversize_identity_messages_have_finite_byte_deadlines(self):
        for payload, error in ((struct.pack("!I", 4097), ValueError), (struct.pack("!I", 10) + b"x", TimeoutError)):
            with self.subTest(error=error):
                parent, child = socket.socketpair()
                try:
                    child.sendall(payload)
                    with self.assertRaises(error):
                        _read_identity(parent, timeout=0.03)
                finally:
                    parent.close()
                    child.close()

    def test_owner_eof_or_invalid_command_cancels_without_process_termination(self):
        for command in (b"", b"C", b"X"):
            with self.subTest(command=command):
                parent, child = socket.socketpair()
                stop, cancel, protocol_error = threading.Event(), threading.Event(), threading.Event()
                watcher = threading.Thread(target=_watch_owner, args=(child, stop, cancel, protocol_error))
                watcher.start()
                if command:
                    parent.sendall(command)
                else:
                    parent.shutdown(socket.SHUT_WR)
                watcher.join(1)
                parent.close()
                child.close()
                self.assertFalse(watcher.is_alive())
                self.assertTrue(cancel.is_set())
                self.assertEqual(protocol_error.is_set(), command == b"X")

    def test_invalid_control_during_drain_never_returns_success_identity(self):
        parent, child = socket.socketpair()
        errors = []
        def drained_runner(**kwargs):
            self.assertTrue(kwargs["cancel_event"].wait(1))
        try:
            with patch("disclosure_anchor.adapters.runtime.dedicated_mac_observer.run_synchronized_telemetry_observer", side_effect=drained_runner):
                worker = threading.Thread(target=self._child, args=(child, _request(), errors))
                worker.start()
                _read_identity(parent, timeout=1)
                parent.sendall(b"GOX")
                with self.assertRaises(EOFError):
                    _read_identity(parent, timeout=2)
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIn("invalid control message", str(errors[0]))
        finally:
            parent.close()
            child.close()

    def test_identity_drift_before_go_prevents_runner_call(self):
        parent, child = socket.socketpair()
        errors, observations = [], iter((_identity_bytes(), b"changed"))
        try:
            with patch("disclosure_anchor.adapters.runtime.dedicated_mac_observer.run_synchronized_telemetry_observer") as run:
                worker = threading.Thread(target=self._child, args=(child, _request(), errors, lambda: next(observations)))
                worker.start()
                _read_identity(parent, timeout=1)
                parent.sendall(b"GO")
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIn("identity changed", str(errors[0]))
                run.assert_not_called()
        finally:
            parent.close()
            child.close()


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("DISCLOSURE_TEST_MAC_OBSERVER_IDENTITY") == "1", "explicit Mac kernel/spawn observer identity opt-in required")
class DedicatedMacObserverNativeTests(unittest.TestCase):
    def test_actual_child_kernel_identity_go_seal_and_reaped_exit(self):
        raw = MacObserverIdentityReader().observe()
        clock = check_mac_observer_identity(raw).clock_domain_identity_sha256
        with tempfile.TemporaryDirectory() as temporary:
            args = _request()
            def spec(lane):
                return replace(_collector_spec(lane=lane, observer_clock_domain_identity_sha256=clock), factory_module=__name__, factory_qualname="_clocked_test_collector_factory")
            request = replace(args, artifact_root=Path(temporary) / "telemetry", gpu_collector=spec("gpu"), host_collector=spec("host"), duration_seconds=2)
            owner = DedicatedMacObserver(request)
            self.assertNotEqual(owner.pid, os.getpid())
            self.assertEqual(json.loads(owner.identity_bytes)["process"]["parent_pid"], os.getpid())
            self.assertFalse(request.artifact_root.exists())
            try:
                owner.start()
                result = owner.poll(timeout=15)
                self.assertIsNotNone(result)
                self.assertEqual(result.receipt.observer_identity, check_mac_observer_identity(owner.identity_bytes))
                self.assertTrue(result.frames)
                self.assertTrue(owner._closed)
                with self.assertRaises(RuntimeError):
                    owner.start()
            finally:
                owner.close()
