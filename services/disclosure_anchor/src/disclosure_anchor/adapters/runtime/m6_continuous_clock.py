"""Sleep-inclusive local guard clock; never an M6 measurement denominator."""

from __future__ import annotations

import ctypes
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

        return now
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    raise RuntimeError("M6 local guard requires a qualified sleep-inclusive clock")
