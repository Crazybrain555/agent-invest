"""Runtime adapters.

Doctor exports stay lazy so importing an unrelated passive runtime adapter does
not also import database drivers and operational checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from disclosure_anchor.adapters.runtime.doctor import (
        CheckResult,
        DoctorReport,
        run_doctor,
        run_startup_preflight,
    )


_DOCTOR_EXPORTS = frozenset(
    {"CheckResult", "DoctorReport", "run_doctor", "run_startup_preflight"}
)


def __getattr__(name: str) -> Any:
    if name not in _DOCTOR_EXPORTS:
        raise AttributeError(name)
    from disclosure_anchor.adapters.runtime import doctor

    return getattr(doctor, name)


def __dir__() -> list[str]:
    return sorted((*globals(), *_DOCTOR_EXPORTS))


__all__ = ["CheckResult", "DoctorReport", "run_doctor", "run_startup_preflight"]
