"""Deterministic native ABI and separately observed process/clock evidence."""

from __future__ import annotations

import copy
import ctypes
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mac_observer_identity import (
    MacObserverIdentityReader, _ProcBsdInfo,
)
from disclosure_anchor.application.contracts.resident_session_evidence import (
    canonical_bytes, check_mac_observer_identity,
)


BOOT = b"12345678-1234-4234-8234-1234567890AB\0"


class _Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class MacObserverIdentityTests(unittest.TestCase):
    def _reader(self, *, boot_size=37, process_size=136, observed_pid=4321):
        def sysctl(name, buffer, size, newp, newlen):
            self.assertEqual((name, newp, newlen), (b"kern.bootsessionuuid", None, 0))
            ctypes.memmove(buffer, BOOT, len(BOOT))
            ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = boot_size
            return 0

        def proc(pid, flavor, arg, output, size):
            self.assertEqual((pid, flavor, arg, size), (4321, 3, 0, 136))
            info = _ProcBsdInfo()
            info.pbi_pid = observed_pid
            info.pbi_ppid = 1234
            info.pbi_uid = 501
            info.pbi_start_tvsec = 1_788_765_035
            info.pbi_start_tvusec = 564385
            ctypes.memmove(output, ctypes.byref(info), ctypes.sizeof(info))
            return process_size

        libraries = [SimpleNamespace(sysctlbyname=_Function(sysctl)), SimpleNamespace(proc_pidinfo=_Function(proc))]
        with patch("disclosure_anchor.adapters.runtime.mac_observer_identity.sys.platform", "darwin"), patch("disclosure_anchor.adapters.runtime.mac_observer_identity.ctypes.CDLL", side_effect=libraries) as load:
            reader = MacObserverIdentityReader()
        self.assertEqual(load.call_args_list[0].args, ("/usr/lib/libSystem.B.dylib",))
        self.assertEqual(load.call_args_list[1].args, ("/usr/lib/libproc.dylib",))
        self.assertTrue(all(call.kwargs == {"use_errno": True} for call in load.call_args_list))
        return reader

    def _observation(self):
        reader = self._reader()
        with patch("disclosure_anchor.adapters.runtime.mac_observer_identity.time.get_clock_info", return_value=SimpleNamespace(implementation="mach_absolute_time()", monotonic=True, adjustable=False, resolution=4.166666666666666e-08)), patch("disclosure_anchor.adapters.runtime.mac_observer_identity.os.uname", return_value=SimpleNamespace(release="25.6.0")):
            raw = reader.observe(4321)
        return reader, raw

    def test_native_identity_is_real_integer_birth_and_separate_shared_clock(self):
        reader, raw = self._observation()
        value = json.loads(raw)
        self.assertEqual(value["process"], reader.process(4321))
        self.assertEqual(value["process"]["start_time_microseconds"], 564385)
        identity = check_mac_observer_identity(raw)
        changed = copy.deepcopy(value)
        changed["process"]["pid"] = 4322
        other = check_mac_observer_identity(canonical_bytes(changed))
        self.assertNotEqual(identity.process_epoch_sha256, other.process_epoch_sha256)
        self.assertEqual(identity.clock_domain_identity_sha256, other.clock_domain_identity_sha256)

    def test_native_short_reads_wrong_pid_and_invalid_arguments_fail_closed(self):
        for args, error in (({"boot_size": 36}, ValueError), ({"process_size": 135}, OSError), ({"observed_pid": 4322}, ValueError)):
            with self.subTest(args=args), self.assertRaises(error):
                self._reader(**args).process(4321)
        for pid in (True, 0, -1, 2**31):
            with self.assertRaises(ValueError):
                self._reader().process(pid)

    def test_syscall_errno_and_non_mac_never_become_synthetic_identity(self):
        reader = self._reader()
        reader._system.sysctlbyname.callback = lambda *args: -1
        with patch("ctypes.get_errno", return_value=1), self.assertRaises(OSError) as caught:
            reader.boot_session_uuid()
        self.assertEqual(caught.exception.errno, 1)
        with patch("disclosure_anchor.adapters.runtime.mac_observer_identity.sys.platform", "linux"), self.assertRaisesRegex(RuntimeError, "64-bit macOS"):
            MacObserverIdentityReader()

    def test_pid_reuse_during_observation_is_not_accepted(self):
        reader, raw = self._observation()
        value = json.loads(raw)
        changed = {**value["process"], "start_time_microseconds": 564386}
        with patch.object(reader, "process", side_effect=[value["process"], changed]), self.assertRaisesRegex(ValueError, "process identity changed"):
            reader.observe(4321)

    def test_rehashed_wrong_clock_boot_or_fabricated_timestamp_is_rejected(self):
        _reader, raw = self._observation()
        for section, key, value in (
            ("process", "start_time_microseconds", 1_000_000),
            ("process", "start_time_unix_seconds", True),
            ("clock", "boot_session_uuid", "22345678-1234-4234-8234-1234567890AB"),
            ("clock", "implementation", "QueryPerformanceCounter"),
            ("clock", "adjustable", True),
            ("clock", "resolution_seconds", 0.0),
        ):
            payload = json.loads(raw)
            payload[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_mac_observer_identity(canonical_bytes(payload))
