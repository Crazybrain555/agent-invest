"""Shared fixtures for the independent F5 operational-stop tests.

Real ``Settings`` over a disposable trusted runtime root, and a scripted
``launchctl`` runner for ``LaunchdSupervisor``. The runner is injected as the
supervisor's command runner with a nonexistent executable path, so no test can
reach the real tool; it refuses any other executable and any label other than
its own disposable ``com.agentinvest.f5-acceptance.<uuid>`` label.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from disclosure_anchor.adapters.runtime.worker_stop_control import (
    LAUNCHCTL_SERVICE_NOT_FOUND_EXIT,
    LaunchdSupervisor,
    RuntimeWorkerStopControl,
    WorkerControlStore,
    WorkerSupervision,
)
from disclosure_anchor.application.ports.worker_stop_control import PublicStopCause
from disclosure_anchor.settings import SENTINEL_NAME, Settings, load_settings
from tests.unit.test_settings import _env


FAKE_LAUNCHCTL = "/nonexistent/f5-acceptance/launchctl"
PRODUCTION_LABEL = "com.agentinvest.disclosure-worker"


def disposable_label() -> str:
    return f"com.agentinvest.f5-acceptance.{uuid.uuid4()}"


def trusted_root(test: unittest.TestCase) -> Path:
    """A temp agent_system-shaped root whose runtime root is trusted (0700, own uid)."""

    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    root = Path(directory.name)
    runtime = root / "services" / "disclosure_anchor" / "runtime"
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    return root


def runtime_root(root: Path) -> Path:
    return root / "services" / "disclosure_anchor" / "runtime"


def control_dir(root: Path) -> Path:
    return runtime_root(root) / "control"


def settings_for(
    root: Path,
    *,
    supervised: bool = False,
    label: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> Settings:
    """Real Settings; supervised binds the supervised root to this temp root."""

    environment = dict(_env(root))
    if supervised:
        environment["DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT"] = environment[
            "DISCLOSURE_RUNTIME_ROOT"
        ]
        (root / SENTINEL_NAME).write_text("", encoding="utf-8")
    if label is not None:
        environment["DISCLOSURE_WORKER_LAUNCHD_LABEL"] = label
    environment.update(extra or {})
    with mock.patch.dict(os.environ, environment, clear=True):
        return load_settings()


def cause(
    reason_code: str = "forbidden_tool_call",
    *,
    kind: str = "semantic_failed_closed",
    origin: str = "stage_call",
    attempt_id: str | None = "rpa_f5_acceptance_a",
) -> PublicStopCause:
    return PublicStopCause(
        kind=kind, reason_code=reason_code, origin=origin, attempt_id=attempt_id,
    )


def listing(directory: Path) -> dict[str, tuple[int, int, int]]:
    """name -> (mode, size, mtime_ns) for every entry, symlinks not followed."""

    if not directory.exists():
        return {}
    result: dict[str, tuple[int, int, int]] = {}
    for entry in sorted(directory.iterdir()):
        info = entry.lstat()
        result[entry.name] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return result


class ScriptedLaunchctl:
    """A fake ``subprocess.run`` for one disposable label; records every argv.

    ``disabled`` is the label's override: ``None`` (unlisted), ``"disabled"``,
    ``"enabled"`` or any other literal. ``loaded`` selects the ``print`` reply:
    ``None`` is the exact not-found pair, otherwise a state line (``running``,
    ``not running``) with ``last_exit``. ``failures`` maps a verb to an
    exception instance or an ``int`` return code.
    """

    def __init__(
        self,
        label: str,
        *,
        uid: int | None = None,
        disabled: str | None = None,
        table: bool = True,
        loaded: str | None = None,
        pid: int | None = None,
        last_exit: str = "(never exited)",
        disable_takes_effect: bool = True,
        failures: Mapping[str, BaseException | int] | None = None,
        before: Callable[[str], None] | None = None,
    ) -> None:
        if label == PRODUCTION_LABEL or not label.startswith("com.agentinvest.f5-acceptance."):
            raise AssertionError("the scripted launchctl serves disposable labels only")
        self.label = label
        self.uid = os.getuid() if uid is None else uid
        self.disabled = disabled
        self.table = table
        self.loaded = loaded
        self.pid = pid
        self.last_exit = last_exit
        self.disable_takes_effect = disable_takes_effect
        self.failures = dict(failures or {})
        self.before = before
        self.calls: list[tuple[str, ...]] = []
        self._lock = threading.Lock()

    def supervisor(self) -> LaunchdSupervisor:
        return LaunchdSupervisor(self.label, uid=self.uid, launchctl=FAKE_LAUNCHCTL, runner=self)

    def supervision(self) -> WorkerSupervision:
        return WorkerSupervision("supervised", self.supervisor())

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] != FAKE_LAUNCHCTL:
            raise AssertionError(f"unexpected launchctl executable {argv[0]!r}")
        if any(PRODUCTION_LABEL in part for part in argv):
            raise AssertionError("a test addressed the production label")
        if not (capture_output and text and not check and timeout > 0):
            raise AssertionError("launchctl must be bounded, captured text, unchecked")
        verb, *rest = argv[1:]
        domain = f"gui/{self.uid}"
        target = f"{domain}/{self.label}"
        expected = {"print-disabled": [domain]}.get(verb, [target])
        if rest != expected:
            raise AssertionError(f"unexpected launchctl arguments {argv[1:]!r}")
        with self._lock:
            self.calls.append(tuple(argv[1:]))
        if self.before is not None:
            self.before(verb)
        failure = self.failures.get(verb)
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, int):
            return subprocess.CompletedProcess(argv, failure, "", "launchctl failed\n")
        if verb == "print-disabled":
            return subprocess.CompletedProcess(argv, 0, self._disabled_table(), "")
        if verb == "print":
            return self._print(argv)
        if verb == "disable":
            if self.disable_takes_effect:
                self.disabled = "disabled"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "enable":
            self.disabled = "enabled"
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected launchctl verb {verb!r}")

    def _disabled_table(self) -> str:
        lines = []
        if self.table:
            lines.append("disabled services = {")
            lines.append('\t"com.apple.f5-acceptance-neighbor" => enabled')
            if self.disabled is not None:
                lines.append(f'\t"{self.label}" => {self.disabled}')
            lines.append("}")
            lines.append("")
            lines.append("login item associations = {")
            lines.append("}")
        elif self.disabled is not None:
            lines.append(f'"{self.label}" => {self.disabled}')
        return "\n".join(lines) + "\n"

    def _print(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if self.loaded is None:
            return subprocess.CompletedProcess(
                argv,
                LAUNCHCTL_SERVICE_NOT_FOUND_EXIT,
                "",
                f'Could not find service "{self.label}" in domain for user gui: {self.uid}\n',
            )
        lines = [f"{self.target} = {{", "\tactive count = 0", f"\tstate = {self.loaded}"]
        if self.pid is not None:
            lines.append(f"\tpid = {self.pid}")
        lines.append(f"\tlast exit code = {self.last_exit}")
        lines.append("}")
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

    @property
    def target(self) -> str:
        return f"gui/{self.uid}/{self.label}"


def durable_control(
    settings: Settings,
    *,
    launchctl: ScriptedLaunchctl | None,
    lines: list[str] | None = None,
    emit: Callable[[str], None] | None = None,
) -> RuntimeWorkerStopControl:
    """The real durable control over these settings with an injected supervisor."""

    sink = lines if lines is not None else []
    return RuntimeWorkerStopControl(
        store=WorkerControlStore.for_settings(settings),
        supervisor=None if launchctl is None else launchctl.supervisor(),
        emit=emit if emit is not None else sink.append,
    )


def write_stop(settings: Settings, reason_code: str = "forbidden_tool_call") -> str:
    """Record one real automatic stop through the durable control; return its hash."""

    control = durable_control(settings, launchctl=None)
    if not control.trip(cause(reason_code)):
        raise AssertionError("fresh control did not accept the first cause")
    outcome = control.persistence_outcome(timeout=5)
    if outcome is None or outcome.marker.status != "written" or outcome.marker.sha256 is None:
        raise AssertionError(f"fixture stop record was not written: {outcome}")
    return outcome.marker.sha256


__all__ = [
    "FAKE_LAUNCHCTL",
    "PRODUCTION_LABEL",
    "ScriptedLaunchctl",
    "cause",
    "control_dir",
    "disposable_label",
    "durable_control",
    "listing",
    "runtime_root",
    "settings_for",
    "trusted_root",
    "write_stop",
]
