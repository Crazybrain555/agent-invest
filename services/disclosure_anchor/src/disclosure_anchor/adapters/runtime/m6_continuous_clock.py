"""Sleep-inclusive local guard clock; never an M6 measurement denominator."""

from __future__ import annotations

import ctypes
import hashlib
import sys
import time
from collections.abc import Callable


def continuous_nanoseconds_clock() -> Callable[[], int]:
    """No uptime-only fallback: admission leases must expire across system sleep.

    Apple documents mach_continuous_time as advancing while asleep; its ticks
    use mach_timebase_info. Linux CLOCK_BOOTTIME has the equivalent guard
    property. Windows measurement itself belongs to the qualified QPC owner.
    """
    if sys.platform == "darwin":
        return _mach_continuous_clock()[0]
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    raise RuntimeError("M6 local guard requires a qualified sleep-inclusive clock")


def _mach_continuous_clock() -> tuple[Callable[[], int], int, int]:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

    class Timebase(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    clock = library.mach_continuous_time
    clock.argtypes = []
    clock.restype = ctypes.c_uint64
    timebase_info = library.mach_timebase_info
    timebase_info.argtypes = [ctypes.POINTER(Timebase)]
    timebase_info.restype = ctypes.c_int
    scale = Timebase()
    if timebase_info(ctypes.byref(scale)) != 0 or not scale.numer or not scale.denom:
        raise RuntimeError("Mach continuous clock timebase unavailable")
    numerator, denominator = int(scale.numer), int(scale.denom)

    def now() -> int:
        return int(clock()) * numerator // denominator

    return now, numerator, denominator


class DiagnosticContinuousClock:
    """Adapter-created boot/timebase identity paired with its actual accessor.

    This prevents accidental caller injection, not hostile in-process mutation.
    Original journal/worker checkpoints own regression and deadline checks.
    """

    __slots__ = ("_descriptor_bytes", "_identity_sha256", "_now")
    _descriptor_bytes: bytes
    _identity_sha256: str
    _now: Callable[[], int]

    def __new__(cls) -> DiagnosticContinuousClock:
        raise TypeError("diagnostic clock requires diagnostic_continuous_clock()")

    @property
    def descriptor_bytes(self) -> bytes:
        return self._descriptor_bytes

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    def now_ns(self) -> int:
        value = self._now()
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("diagnostic continuous clock sample is outside int63")
        return value


def diagnostic_continuous_clock() -> DiagnosticContinuousClock:
    """Acquire the actual Mac boot and Mach timebase; never infer or fall back.

    Parent and worker acquire this independently and compare its canonical hash
    to the original journal/input. Acquisition creates no new deadline.
    """
    if sys.platform != "darwin":
        raise RuntimeError("owned diagnostic continuous clock requires macOS")

    from disclosure_anchor.adapters.runtime.mac_observer_identity import (
        MacObserverIdentityReader,
    )
    from disclosure_anchor.application.contracts.diagnostic_json import (
        bounded_json_bytes,
    )

    reader = MacObserverIdentityReader()
    boot_before = reader.boot_session_uuid()
    now, numerator, denominator = _mach_continuous_clock()
    if reader.boot_session_uuid() != boot_before:
        raise ValueError("macOS boot changed during continuous clock acquisition")
    descriptor = bounded_json_bytes({
        "contract_version": "mineru-diagnostic-continuous-clock.v1",
        "platform": "darwin",
        "clock_api": "mach_continuous_time",
        "boot_session_uuid": boot_before,
        "timebase_numer": numerator,
        "timebase_denom": denominator,
        "output_unit": "nanoseconds",
        "conversion": "floor(ticks*numer/denom)",
    }, maximum_bytes=1024)
    result = object.__new__(DiagnosticContinuousClock)
    result._descriptor_bytes = descriptor
    result._identity_sha256 = "sha256:" + hashlib.sha256(descriptor).hexdigest()
    result._now = now
    return result
