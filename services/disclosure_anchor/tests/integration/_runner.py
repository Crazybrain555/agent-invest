"""Run PostgreSQL integration tests in one disposable database.

The configured runtime database is used only as a maintenance connection for
creating a pristine sibling database. The unittest child receives only the
scratch URL and temporary filesystem roots, so committed fixtures are never
visible to the resident production worker.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import FrameType

import sqlalchemy

from disclosure_anchor.adapters.db.postgres.bootstrap import (
    ensure_schemas_and_base_grants,
)
from scripts.managed_scratch_database import (
    ManagedScratchDatabase,
)


_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_ENV_KEYS = (
    "DISCLOSURE_TEST_DATABASE_URL",
    "DISCLOSURE_MIGRATION_DATABASE_URL",
    "DATABASE_URL",
    "DISCLOSURE_ADMIN_DATABASE_URL",
    "DISCLOSURE_READER_DATABASE_URL",
)
_MINERU_RUNTIME_ENV_KEYS = (
    "DISCLOSURE_MINERU_BIN",
    "DISCLOSURE_MINERU_BACKEND",
    "DISCLOSURE_MINERU_SERVER_URL",
)

class ScratchIntegrationDatabase(ManagedScratchDatabase):
    """Provision, lease, migrate, and destroy one integration-test database."""

    def __init__(self, base_url: str, *, real_mineru: bool = False) -> None:
        super().__init__(base_url)
        self._real_mineru = real_mineru
        self._roots = tempfile.TemporaryDirectory(prefix="disclosure-itest-")

    def __enter__(self) -> dict[str, str]:
        try:
            self._provision()
            environment = self._test_environment()
            self._migrate(environment)
            print(
                f"[integration-db] ready {self.database_name} "
                "(production database remains untouched)",
                flush=True,
            )
            return environment
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _provision(self) -> None:
        self.provision()
        schema_engine = sqlalchemy.create_engine(
            self.database_url,
            isolation_level="AUTOCOMMIT",
        )
        try:
            ensure_schemas_and_base_grants(schema_engine)
        finally:
            schema_engine.dispose()

    def _test_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in _DATABASE_ENV_KEYS:
            environment[key] = self.database_url
        root = Path(self._roots.name)
        data_root = root / "services" / "disclosure_anchor"
        shared_root = root / "shared"
        environment.update(
            {
                "DISCLOSURE_DATA_ROOT": str(data_root),
                "DISCLOSURE_SHARED_ROOT": str(shared_root),
                "DISCLOSURE_RUNTIME_ROOT": str(data_root / "runtime"),
                "PYTHONPATH": "src",
            }
        )
        if not self._real_mineru:
            for key in _MINERU_RUNTIME_ENV_KEYS:
                environment.pop(key, None)
            # MinerU has a PATH fallback when its explicit binary is absent.
            # A known-missing scratch path makes accidental parser execution
            # fail locally instead of reaching the resident GPU service.
            environment.update(
                {
                    "DISCLOSURE_MINERU_BIN": str(
                        root / "disabled-mineru" / "mineru"
                    ),
                    "MINERU_MODEL_CACHE": str(
                        shared_root / "model_cache" / "mineru"
                    ),
                    "HF_HOME": str(
                        shared_root / "model_cache" / "huggingface"
                    ),
                    "MODELSCOPE_CACHE": str(
                        shared_root / "model_cache" / "modelscope"
                    ),
                    "WORKER_PARSE_CONCURRENCY": "1",
                }
            )
        return environment

    def _migrate(self, environment: dict[str, str]) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_SERVICE_ROOT,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"scratch database migration failed with exit {completed.returncode}"
            )
        loaded = subprocess.run(
            [sys.executable, "scripts/load_classification_rules.py"],
            cwd=_SERVICE_ROOT,
            env=environment,
            check=False,
        )
        if loaded.returncode != 0:
            raise RuntimeError(
                "scratch classification-rule load failed with exit "
                f"{loaded.returncode}"
            )

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._roots.cleanup()


def _signal_process_group(child: subprocess.Popen[bytes], signum: int) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _run_unittest_child(
    environment: dict[str, str],
    *,
    test_names: Sequence[str],
    verbose: bool,
) -> int:
    command = [sys.executable, "-m", "unittest"]
    if verbose:
        command.append("-v")
    if test_names:
        command.extend(test_names)
    else:
        command.extend(
            ["discover", "-s", "tests/integration", "-t", ".", "-p", "test_*.py"]
        )
    child = subprocess.Popen(
        command,
        cwd=_SERVICE_ROOT,
        env=environment,
        start_new_session=True,
    )
    try:
        return child.wait()
    except BaseException:
        if child.poll() is None:
            # Let unittest unwind the active test first. MinerUProcess catches
            # BaseException and stops its detached CLI/API process groups.
            _signal_process_group(child, signal.SIGINT)
            try:
                child.wait(timeout=65)
            except subprocess.TimeoutExpired:
                _signal_process_group(child, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _signal_process_group(child, signal.SIGKILL)
                    child.wait()
        raise


def _termination_requested(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def _base_database_url() -> str | None:
    return os.environ.get("DISCLOSURE_MIGRATION_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tests", nargs="*", help="optional unittest dotted names")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--real-mineru",
        action="store_true",
        help=(
            "preserve the explicit MinerU runtime/server/cache environment "
            "for GPU-backed smoke tests"
        ),
    )
    args = parser.parse_args(argv)
    base_url = _base_database_url()
    if not base_url:
        print("[skip] no PostgreSQL runtime URL available for scratch provisioning")
        return 0

    previous_sigterm = signal.signal(signal.SIGTERM, _termination_requested)
    try:
        with ScratchIntegrationDatabase(
            base_url, real_mineru=bool(args.real_mineru)
        ) as environment:
            return _run_unittest_child(
                environment,
                test_names=args.tests,
                verbose=bool(args.verbose),
            )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
