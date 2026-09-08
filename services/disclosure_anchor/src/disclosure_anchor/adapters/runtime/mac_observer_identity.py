"""Read actual macOS observer process birth and monotonic clock identity.

No shell, ps, sysctl process, guessed boot time or synthetic process timestamp.
The outer owner reads the same kernel identity for its explicitly spawned child.
The native ABI is the installed Apple SDK's libproc.h and sys/proc_info.h
PROC_PIDTBSDINFO; a short or incompatible result fails closed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from typing import Any
import uuid

from disclosure_anchor.application.contracts.resident_session_evidence import (
    canonical_bytes,
    check_mac_observer_identity,
)


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        *((name, ctypes.c_uint32) for name in (
            "pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid",
            "pbi_uid", "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid", "pbi_svgid", "rfu_1",
        )),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        *((name, ctypes.c_uint32) for name in (
            "pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid",
        )),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class MacObserverIdentityReader:
    """One explicitly created, read-only native identity reader, never per tick."""

    def __init__(self) -> None:
        if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("observer identity requires 64-bit macOS")
        if ctypes.sizeof(_ProcBsdInfo) != 136 or _ProcBsdInfo.pbi_start_tvsec.offset != 120:
            raise RuntimeError("macOS proc_bsdinfo ABI layout differs")
        self._system: Any = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self._proc: Any = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._system.sysctlbyname.argtypes = [
            ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p, ctypes.c_size_t,
        ]
        self._system.sysctlbyname.restype = ctypes.c_int
        self._proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self._proc.proc_pidinfo.restype = ctypes.c_int

    def boot_session_uuid(self) -> str:
        # UUID text plus NUL. newp=NULL/newlen=0 is a read, never a sysctl write.
        buffer = ctypes.create_string_buffer(37)
        size = ctypes.c_size_t(ctypes.sizeof(buffer))
        result = self._system.sysctlbyname(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0)
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, "reading macOS boot session identity failed")
        if size.value != 37 or buffer.raw[-1:] != b"\0" or b"\0" in buffer.raw[:-1]:
            raise ValueError("macOS boot session UUID length differs")
        value = buffer.raw[:-1].decode("ascii", errors="strict")
        if str(uuid.UUID(value)).upper() != value.upper():
            raise ValueError("macOS boot session UUID is not canonical")
        return value  # preserve the exact OS spelling in the private evidence

    def process(self, pid: int) -> dict[str, object]:
        if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
            raise ValueError("macOS process PID is invalid")
        boot_before = self.boot_session_uuid()
        info = _ProcBsdInfo()
        size = ctypes.sizeof(info)
        result = self._proc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        if result != size:
            code = ctypes.get_errno()
            raise OSError(code, f"macOS process identity byte count differs: {result}")
        if info.pbi_pid != pid or info.pbi_start_tvsec == 0 or info.pbi_start_tvusec >= 1_000_000:
            raise ValueError("macOS process identity is invalid")
        if self.boot_session_uuid() != boot_before:
            raise ValueError("macOS boot changed during process observation")
        return {
            "pid": int(info.pbi_pid), "parent_pid": int(info.pbi_ppid), "uid": int(info.pbi_uid),
            "start_time_unix_seconds": int(info.pbi_start_tvsec),
            "start_time_microseconds": int(info.pbi_start_tvusec),
            "boot_session_uuid": boot_before,
        }

    def observe(self, pid: int | None = None) -> bytes:
        """Observe current child or independently inspect the owned child's PID."""
        target = os.getpid() if pid is None else pid
        process = self.process(target)
        clock = time.get_clock_info("monotonic")
        payload = canonical_bytes({
            "contract_version": "mineru.mac-observer-identity.v1",
            "process": process,
            "clock": {
                "boot_session_uuid": self.boot_session_uuid(), "kernel_release": os.uname().release,
                "implementation": clock.implementation, "monotonic": clock.monotonic,
                "adjustable": clock.adjustable, "resolution_seconds": clock.resolution,
            },
        })
        if self.process(target) != process:
            raise ValueError("macOS process identity changed during observation")
        check_mac_observer_identity(payload)
        return payload


__all__ = ["MacObserverIdentityReader"]
