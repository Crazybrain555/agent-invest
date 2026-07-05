"""Environment diagnostics: report configuration and dependency readiness.

Diagnostic only — warnings do not fail the process; a broken envelope-kernel install does.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from asset_intake.settings import Settings, get_settings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def collect_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    try:
        from envelope_kernel import CONTRACT_VERSION

        checks.append(Check("envelope_kernel", True, f"importable, contract {CONTRACT_VERSION}"))
    except ImportError as exc:
        checks.append(Check("envelope_kernel", False, f"import failed: {exc} (run make venv)"))

    checks.append(
        Check(
            "data_root",
            settings.data_root.is_dir(),
            f"{settings.data_root}"
            + ("" if settings.data_root.is_dir() else " missing (AgentSSD not mounted?)"),
        )
    )

    for name, value in (
        ("database_url", settings.database_url),
        ("migration_database_url", settings.migration_database_url),
        ("reader_database_url", settings.reader_database_url),
    ):
        checks.append(
            Check(name, value is not None, "set" if value is not None else "unset (live-DB steps unavailable)")
        )

    checks.append(
        Check(
            "tushare_token",
            settings.tushare_token is not None,
            "set" if settings.tushare_token is not None else "unset (real-provider smoke will skip)",
        )
    )
    return checks


def main() -> int:
    checks = collect_checks(get_settings())
    for check in checks:
        marker = "ok" if check.ok else "warn"
        print(f"[{marker}] {check.name}: {check.detail}")
    kernel = checks[0]
    return 0 if kernel.ok else 1


if __name__ == "__main__":
    sys.exit(main())
