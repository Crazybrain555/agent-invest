"""Controlled libSystem/libproc calls; no native clock qualification claim."""

from contextlib import contextmanager
import ctypes
from types import SimpleNamespace
from unittest.mock import patch


MODULE = "disclosure_anchor.adapters.runtime.m6_continuous_clock"
BOOT = "12345678-1234-4234-8234-1234567890AB"
OTHER_BOOT = "22345678-1234-4234-8234-1234567890AB"
EXPECTED_DESCRIPTOR = (
    b'{"boot_session_uuid":"12345678-1234-4234-8234-1234567890AB",'
    b'"clock_api":"mach_continuous_time",'
    b'"contract_version":"mineru-diagnostic-continuous-clock.v1",'
    b'"conversion":"floor(ticks*numer/denom)",'
    b'"output_unit":"nanoseconds","platform":"darwin",'
    b'"timebase_denom":3,"timebase_numer":125}'
)


class NativeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


class NativeClockFixture:
    """Exercise actual production ctypes setup using in-memory native functions."""

    def __init__(self, *, numer=125, denom=3, ticks=(3,), boots=(BOOT, BOOT),
                 timebase_status=0, timebase_error=None, boot_error=None,
                 tick_error=None, load_error=None, boot_size=37, boot_status=0):
        self.numer, self.denom = numer, denom
        self.ticks, self.boots = list(ticks), list(boots)
        self.timebase_status, self.timebase_error = timebase_status, timebase_error
        self.boot_error, self.tick_error, self.load_error = boot_error, tick_error, load_error
        self.boot_size, self.boot_status = boot_size, boot_status
        self.events, self.loads = [], []
        self.timebase_type = None
        self.mach = NativeFunction(self._tick)
        self.timebase = NativeFunction(self._timebase)
        self.sysctl = NativeFunction(self._boot)
        self.proc = NativeFunction(self._forbidden_process)
        self.system = SimpleNamespace(mach_continuous_time=self.mach,
                                      mach_timebase_info=self.timebase, sysctlbyname=self.sysctl)
        self.process = SimpleNamespace(proc_pidinfo=self.proc)

    def _tick(self):
        self.events.append("tick")
        if self.tick_error is not None:
            raise self.tick_error
        value = self.ticks[0]
        if len(self.ticks) > 1:
            self.ticks.pop(0)
        return value

    def _timebase(self, pointer):
        self.events.append("timebase")
        if self.timebase_error is not None:
            raise self.timebase_error
        self.timebase_type = type(pointer._obj)
        pointer._obj.numer = self.numer
        pointer._obj.denom = self.denom
        return self.timebase_status

    def _boot(self, name, buffer, size, newp, newlen):
        self.events.append("boot")
        if self.boot_error is not None:
            raise self.boot_error
        if (name, newp, newlen) != (b"kern.bootsessionuuid", None, 0):
            raise AssertionError("expected existing read-only boot sysctl")
        value = self.boots[0]
        if len(self.boots) > 1:
            self.boots.pop(0)
        raw = value.encode("ascii") + b"\0"
        if len(raw) != 37:
            raise AssertionError("fixture uses exactly 37 response bytes")
        ctypes.memmove(buffer, raw, len(raw))
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = self.boot_size
        return self.boot_status

    def _forbidden_process(self, *_args):
        raise AssertionError("clock acquisition must not observe a process")

    def load(self, path, **kwargs):
        self.loads.append((path, kwargs))
        if self.load_error is not None:
            raise self.load_error
        if path == "/usr/lib/libSystem.B.dylib":
            return self.system
        if path == "/usr/lib/libproc.dylib":
            return self.process
        raise AssertionError("unexpected native library: " + str(path))

    @contextmanager
    def install(self):
        with patch(MODULE + ".sys.platform", "darwin"), patch("ctypes.CDLL", side_effect=self.load):
            yield self
