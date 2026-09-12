"""Independent owned clock accessor tests using controlled native boundaries.

Real Mach/sysctl execution belongs only to the separate opt-in probe. These
tests exercise neither owned lifecycle entry nor child/quality authority.
"""

import ctypes
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import m6_continuous_clock as clock_module
from disclosure_anchor.adapters.runtime.m6_continuous_clock import (
    DiagnosticContinuousClock, continuous_nanoseconds_clock, diagnostic_continuous_clock,
)
from tests._mineru_diagnostic_clock_fixture import (
    BOOT, EXPECTED_DESCRIPTOR, MODULE, OTHER_BOOT, NativeClockFixture,
)


class DiagnosticClockTests(unittest.TestCase):
    def test_old_mac_callable_preserves_integer_floor_and_large_tick_precision(self):
        cases = (
            (125, 3, (0, 1, 2, 3, 4), (0, 41, 83, 125, 166)),
            (3, 2, (9007199254740993,), (13510798882111489,)),
            (1, 3, (18446744073709551615,), (6148914691236517205,)),
        )
        for numer, denom, ticks, expected in cases:
            with self.subTest(numer=numer, denom=denom):
                native = NativeClockFixture(numer=numer, denom=denom, ticks=ticks)
                with native.install():
                    clock = continuous_nanoseconds_clock()
                    self.assertEqual(tuple(clock() for _ in expected), expected)
                self.assertEqual(native.loads, [("/usr/lib/libSystem.B.dylib", {})])
                self.assertEqual(native.sysctl.calls, [])
                self.assertEqual(native.proc.calls, [])

    def test_old_linux_callable_remains_actual_boottime_and_does_not_load_mac(self):
        calls = []

        def read(clock_id):
            calls.append(clock_id)
            return (101, 205)[len(calls) - 1]

        fake_time = SimpleNamespace(CLOCK_BOOTTIME=77, clock_gettime_ns=read)
        with patch(MODULE + ".sys.platform", "linux"), patch(MODULE + ".time", fake_time), patch(
            "ctypes.CDLL", side_effect=AssertionError("Linux legacy must not load Mac library")
        ):
            clock = continuous_nanoseconds_clock()
            self.assertEqual((clock(), clock()), (101, 205))
        self.assertEqual(calls, [77, 77])

    def test_old_unsupported_or_linux_without_boottime_has_no_fallback(self):
        for platform in ("win32", "freebsd", "linux"):
            with self.subTest(platform=platform), patch(MODULE + ".sys.platform", platform), patch(
                MODULE + ".time", SimpleNamespace()
            ), patch("ctypes.CDLL", side_effect=AssertionError("must fail before native load")):
                with self.assertRaises(RuntimeError):
                    continuous_nanoseconds_clock()

    def test_both_factories_configure_actual_shared_mach_abi_once_per_acquisition(self):
        for factory in (continuous_nanoseconds_clock, diagnostic_continuous_clock):
            with self.subTest(factory=factory.__name__):
                native = NativeClockFixture()
                with native.install():
                    result = factory()
                    observed = result() if factory is continuous_nanoseconds_clock else result.now_ns()
                self.assertEqual(observed, 125)
                self.assertEqual(native.mach.argtypes, [])
                self.assertIs(native.mach.restype, ctypes.c_uint64)
                self.assertIs(native.timebase.restype, ctypes.c_int)
                structure = native.timebase_type
                self.assertEqual(structure._fields_, [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)])
                self.assertEqual((ctypes.sizeof(structure), structure.numer.offset, structure.denom.offset), (8, 0, 4))
                self.assertEqual(native.timebase.argtypes, [ctypes.POINTER(structure)])
                self.assertEqual(len(native.timebase.calls), 1)

    def test_owned_descriptor_is_exact_literal_canonical_bytes_and_separate_hash_domain(self):
        native = NativeClockFixture()
        with native.install():
            clock = diagnostic_continuous_clock()
            self.assertIs(type(clock), DiagnosticContinuousClock)
            self.assertIs(type(clock.descriptor_bytes), bytes)
            self.assertEqual(clock.descriptor_bytes, EXPECTED_DESCRIPTOR)
            self.assertEqual(clock.identity_sha256, "sha256:" + sha256(EXPECTED_DESCRIPTOR).hexdigest())
            self.assertEqual(clock.now_ns(), 125)
        self.assertEqual([event for event in native.events if event != "tick"], ["boot", "timebase", "boot"])
        self.assertEqual(len(native.sysctl.calls), 2)
        self.assertEqual(native.proc.calls, [])
        self.assertLessEqual(len(clock.descriptor_bytes), 1024)
        self.assertNotEqual(clock.identity_sha256, "sha256:" + sha256(BOOT.encode()).hexdigest())

    def test_separate_acquisitions_same_facts_ignore_tick_samples_and_do_not_share_accessor(self):
        first = NativeClockFixture(ticks=(1, 2))
        second = NativeClockFixture(ticks=(300, 303))
        with first.install():
            left = diagnostic_continuous_clock()
        with second.install():
            right = diagnostic_continuous_clock()
        self.assertIsNot(left, right)
        self.assertEqual((left.descriptor_bytes, left.identity_sha256), (right.descriptor_bytes, right.identity_sha256))
        self.assertEqual((left.now_ns(), left.now_ns()), (41, 83))
        self.assertEqual((right.now_ns(), right.now_ns()), (12500, 12625))

    def test_changed_boot_or_actual_unreduced_timebase_changes_identity(self):
        values = []
        for kwargs in ({}, {"boots": (OTHER_BOOT, OTHER_BOOT)}, {"numer": 250, "denom": 6}, {"numer": 124}):
            native = NativeClockFixture(**kwargs)
            with native.install():
                clock = diagnostic_continuous_clock()
            values.append(clock)
        self.assertEqual(len({clock.identity_sha256 for clock in values}), 4)
        self.assertEqual(values[0].now_ns(), values[2].now_ns(), "equal scale need not erase actual timebase identity")
        self.assertEqual(json.loads(values[2].descriptor_bytes)["timebase_numer"], 250)
        self.assertEqual(json.loads(values[2].descriptor_bytes)["timebase_denom"], 6)

    def test_boot_uuid_exact_os_case_is_retained_and_before_after_drift_is_rejected(self):
        native = NativeClockFixture(boots=(BOOT.lower(), BOOT.lower()))
        with native.install():
            clock = diagnostic_continuous_clock()
        self.assertEqual(json.loads(clock.descriptor_bytes)["boot_session_uuid"], BOOT.lower())
        drift = NativeClockFixture(boots=(BOOT, OTHER_BOOT))
        with drift.install(), self.assertRaises(ValueError):
            diagnostic_continuous_clock()
        self.assertEqual([event for event in drift.events if event != "tick"], ["boot", "timebase", "boot"])

    def test_owned_factory_is_darwin_only_even_when_legacy_linux_clock_exists(self):
        for platform in ("linux", "win32", "freebsd"):
            with self.subTest(platform=platform), patch(MODULE + ".sys.platform", platform), patch(
                MODULE + ".time", SimpleNamespace(CLOCK_BOOTTIME=77, clock_gettime_ns=lambda _: 123)
            ), patch("ctypes.CDLL", side_effect=AssertionError("unsupported owned host loaded native library")):
                with self.assertRaises(RuntimeError):
                    diagnostic_continuous_clock()

    def test_failed_timebase_or_zero_scale_is_rejected_by_both_factories(self):
        for factory in (continuous_nanoseconds_clock, diagnostic_continuous_clock):
            for kwargs in ({"timebase_status": 1}, {"numer": 0}, {"denom": 0}):
                with self.subTest(factory=factory.__name__, kwargs=kwargs):
                    native = NativeClockFixture(**kwargs)
                    with native.install(), self.assertRaises(RuntimeError):
                        factory()
                    self.assertEqual(native.mach.calls, [], "unusable scale sampled a tick")

    def test_original_library_boot_timebase_and_tick_errors_remain_visible(self):
        for field in ("load_error", "boot_error", "timebase_error", "tick_error"):
            primary = OSError(5, "literal " + field)
            native = NativeClockFixture(**{field: primary})
            with self.subTest(field=field), native.install(), self.assertRaises(OSError) as caught:
                clock = diagnostic_continuous_clock()
                clock.now_ns()
            self.assertIs(caught.exception, primary)
        primary = OSError(5, "literal legacy timebase error")
        native = NativeClockFixture(timebase_error=primary)
        with native.install(), self.assertRaises(OSError) as caught:
            continuous_nanoseconds_clock()
        self.assertIs(caught.exception, primary)

    def test_real_existing_boot_reader_rejects_short_uuid_and_syscall_errno(self):
        short = NativeClockFixture(boot_size=36)
        with short.install(), self.assertRaises(ValueError):
            diagnostic_continuous_clock()
        failed = NativeClockFixture(boot_status=-1)
        with failed.install(), patch("ctypes.get_errno", return_value=13), self.assertRaises(OSError) as caught:
            diagnostic_continuous_clock()
        self.assertEqual(caught.exception.errno, 13)
        malformed = NativeClockFixture(boots=("z" * 36, "z" * 36))
        with malformed.install(), self.assertRaises(ValueError):
            diagnostic_continuous_clock()

    def test_private_construction_and_factory_arguments_cannot_supply_identity_or_callable(self):
        for arguments in ((), (lambda: 0,), (EXPECTED_DESCRIPTOR, lambda: 0)):
            with self.subTest(arguments=len(arguments)), self.assertRaises(TypeError):
                DiagnosticContinuousClock(*arguments)
        with self.assertRaises(TypeError):
            diagnostic_continuous_clock(continuous_ns=lambda: 0)
        with self.assertRaises(TypeError):
            diagnostic_continuous_clock(descriptor_bytes=EXPECTED_DESCRIPTOR)
        native = NativeClockFixture()
        with native.install():
            clock = diagnostic_continuous_clock()
        for name, replacement in (("descriptor_bytes", b"{}"), ("identity_sha256", "sha256:" + "0" * 64)):
            with self.subTest(attribute=name), self.assertRaises((AttributeError, TypeError)):
                setattr(clock, name, replacement)
        self.assertEqual(clock.descriptor_bytes, EXPECTED_DESCRIPTOR)

    def test_owned_now_int63_endpoints_and_overflow_use_original_mach_conversion(self):
        for tick in (0, 2**63 - 1):
            native = NativeClockFixture(numer=1, denom=1, ticks=(tick,))
            with self.subTest(tick=tick), native.install():
                clock = diagnostic_continuous_clock()
                self.assertIs(type(clock.now_ns()), int)
                self.assertEqual(clock.now_ns(), tick)
        native = NativeClockFixture(numer=1, denom=1, ticks=(2**63,))
        with native.install(), self.assertRaises(ValueError):
            clock = diagnostic_continuous_clock()
            clock.now_ns()

    def test_accessor_checks_callback_exact_type_before_accepting_int63_sample(self):
        # Bounded fault seam after native setup: C integer conversion itself
        # cannot return Python bool/float, so do not invent a C ABI predicate.
        for value in (False, True, -1, 2**63, 1.5, "1", None):
            native = NativeClockFixture()
            with self.subTest(value=value), native.install(), patch.object(
                clock_module, "_mach_continuous_clock", return_value=(lambda value=value: value, 125, 3)
            ):
                clock = diagnostic_continuous_clock()
                with self.assertRaises(ValueError):
                    clock.now_ns()
                self.assertEqual(clock.descriptor_bytes, EXPECTED_DESCRIPTOR)

    def test_both_public_factories_call_one_shared_native_setup_with_original_returned_scale(self):
        native = NativeClockFixture(numer=7, denom=2, ticks=(5,))
        with native.install(), patch.object(
            clock_module, "_mach_continuous_clock", wraps=clock_module._mach_continuous_clock
        ) as setup:
            old = continuous_nanoseconds_clock()
            owned = diagnostic_continuous_clock()
            self.assertEqual(setup.call_count, 2)
            self.assertEqual((old(), owned.now_ns()), (17, 17))
            self.assertEqual(json.loads(owned.descriptor_bytes)["timebase_numer"], 7)
            self.assertEqual(json.loads(owned.descriptor_bytes)["timebase_denom"], 2)
        self.assertEqual(len(native.timebase.calls), 2)


if __name__ == "__main__":
    unittest.main()
