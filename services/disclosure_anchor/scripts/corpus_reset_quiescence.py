"""Fail-closed worker/GC quiescence checks for destructive corpus reset."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from scripts.corpus_reparse_manifest import ManifestError
from disclosure_anchor.application.worker.locks import WORKER_NS


WORKER_LABEL = "com.agentinvest.disclosure-worker"
GC_LABEL = "com.agentinvest.disclosure-gc"
_PROCESS_MARKERS = (
    "/bin/mineru -p ",
    " -m mineru.cli.fast_api ",
    " -m disclosure_anchor.cli.worker loop",
    " -m disclosure_anchor.cli.worker once",
    "scripts/reparse_corpus.py --run",
)


class Launchctl:
    """Read-only adapter for the exact launchd state reset must prove."""

    def __init__(self, executable: Path = Path("/bin/launchctl")) -> None:
        self._executable = executable

    def _run(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                (str(self._executable), *arguments),
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise ManifestError(f"cannot execute launchctl: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ManifestError(
                f"launchctl {' '.join(arguments)} failed: {detail}"
            ) from exc

    def is_loaded(self, domain: str, label: str) -> bool:
        return (
            self._run("print", f"{domain}/{label}", check=False).returncode
            == 0
        )

    def is_disabled(self, domain: str, label: str) -> bool:
        output = self._run("print-disabled", domain).stdout
        match = re.search(
            rf'"{re.escape(label)}"\s*=>\s*(true|false)\b',
            output,
        )
        return match is not None and match.group(1) == "true"


def running_conflicts(
    *,
    process_rows: Iterable[str] | None = None,
) -> list[str]:
    if process_rows is None:
        try:
            completed = subprocess.run(
                ("/bin/ps", "-axo", "pid=,command="),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ManifestError(
                f"cannot inspect running processes: {exc}"
            ) from exc
        process_rows = completed.stdout.splitlines()
    return [
        row.strip()
        for row in process_rows
        if any(marker in row for marker in _PROCESS_MARKERS)
    ]


def assert_destructive_services_quiescent(
    *,
    launchctl: Launchctl | None = None,
    process_rows: Iterable[str] | None = None,
) -> None:
    """Require persistent disable, unload, and process-zero before deletion."""

    controller = launchctl or Launchctl()
    domain = f"gui/{os.getuid()}"
    failures: list[str] = []
    for label in (WORKER_LABEL, GC_LABEL):
        if controller.is_loaded(domain, label):
            failures.append(f"{label} is still loaded")
        if not controller.is_disabled(domain, label):
            failures.append(f"{label} is not persistently disabled")
    conflicts = running_conflicts(process_rows=process_rows)
    if conflicts:
        failures.append(
            "conflicting worker/MinerU/reparse processes: "
            f"{conflicts!r}"
        )
    if failures:
        raise ManifestError(
            "destructive reset requires quiescent worker and GC: "
            + "; ".join(failures)
        )


@contextmanager
def worker_singleton_lock(database_url: str) -> Iterator[Connection]:
    """Hold the same session advisory lock as the resident worker."""

    engine = sqlalchemy.create_engine(
        database_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    connection = engine.connect()
    try:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:namespace, 0)"),
            {"namespace": WORKER_NS},
        ).scalar_one()
        if not acquired:
            raise ManifestError(
                "worker singleton lock is held; stop and drain the resident worker"
            )
        yield connection
    finally:
        connection.close()
        engine.dispose()
