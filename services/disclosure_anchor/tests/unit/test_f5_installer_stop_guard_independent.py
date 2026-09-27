"""Independent F5 acceptance: installer and restart never release or enable.

Pro §6.1: ``install_launchd.sh`` and ``make worker-restart`` must not bypass a
recorded, invalid or unverifiable stop, and must not implicitly re-enable a
natively disabled label. The unmodified installer runs inside the existing
disposable sandbox (fake launchctl/pgrep on a rebuilt PATH); the Makefile
restart is exercised only on its refusing path, so no test can reach a
kickstart.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.worker_stop_control import (
    RuntimeWorkerStopControl,
    WorkerControlStore,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.settings import Settings, load_settings
from disclosure_anchor.settings import SENTINEL_NAME
from tests.unit._f5_stop_fixture import cause, disposable_label
from tests.unit.test_install_launchd import LABEL, READ_ONLY_LAUNCHCTL, _InstallerSandbox


_SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _sandbox_roots(sandbox: _InstallerSandbox) -> dict[str, str]:
    return {
        "DISCLOSURE_DATA_ROOT": str(sandbox.root / "data"),
        "DISCLOSURE_SHARED_ROOT": str(sandbox.root / "shared"),
        "DISCLOSURE_RUNTIME_ROOT": str(sandbox.runtime_root),
        "MINERU_MODEL_CACHE": str(sandbox.root / "models"),
        "HF_HOME": str(sandbox.root / "hf"),
        "MODELSCOPE_CACHE": str(sandbox.root / "modelscope"),
    }


def _sandbox_settings(sandbox: _InstallerSandbox) -> Settings:
    with mock.patch.dict(os.environ, _sandbox_roots(sandbox), clear=True):
        return load_settings()


def _record_stop(sandbox: _InstallerSandbox) -> str:
    settings = _sandbox_settings(sandbox)
    control = RuntimeWorkerStopControl(
        store=WorkerControlStore(FileStorePathBuilder(settings), runtime_root=sandbox.runtime_root),
        supervisor=None,
        emit=lambda _line: None,
    )
    control.trip(cause())
    outcome = control.persistence_outcome(timeout=5)
    assert outcome is not None and outcome.marker.sha256 is not None
    return outcome.marker.sha256


class InstallerStopGuardTests(unittest.TestCase):
    def _assert_refused_before_mutation(self, sandbox: _InstallerSandbox,
                                        completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(completed.returncode, 78, completed.stderr)
        self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)
        self.assertEqual([path.name for path in sandbox.launch_agents.iterdir()], [sandbox.plist.name])
        commands = sandbox.launchctl_commands()
        self.assertLessEqual({command.split()[0] for command in commands}, READ_ONLY_LAUNCHCTL)
        self.assertNotIn("rollback", completed.stderr)

    def test_recorded_invalid_or_unverifiable_stop_refuses_before_any_launchd_or_plist_change(self) -> None:
        for flag in ((), ("--confirm-operator-disabled",)):
            with self.subTest(state="PUBLIC_STOP", flag=flag):
                sandbox = _InstallerSandbox(self)
                sha = _record_stop(sandbox)
                completed = sandbox.install(arguments=flag)
                self._assert_refused_before_mutation(sandbox, completed)
                self.assertIn(f"PUBLIC_STOP ({sha})", completed.stderr)
                self.assertFalse(sandbox.logs.exists(), "refused before the launchd log directory")
        with self.subTest(state="INVALID_STOP"):
            sandbox = _InstallerSandbox(self)
            control = sandbox.runtime_root / "control"
            control.mkdir(mode=0o700)
            (control / "worker-circuit-stop.json").write_bytes(b"{truncated")
            (control / "worker-circuit-stop.json").chmod(0o600)
            completed = sandbox.install(arguments=("--confirm-operator-disabled",))
            self._assert_refused_before_mutation(sandbox, completed)
            self.assertIn("INVALID_STOP", completed.stderr)
        with self.subTest(state="CONTROL_UNAVAILABLE"):
            sandbox = _InstallerSandbox(self)
            sandbox.runtime_root.rmdir()
            completed = sandbox.install(arguments=("--confirm-operator-disabled",))
            self._assert_refused_before_mutation(sandbox, completed)
            self.assertIn("CONTROL_UNAVAILABLE", completed.stderr)
            self.assertFalse(sandbox.runtime_root.exists(), "the installer never creates the runtime root")

    def test_disabled_label_is_never_re_enabled_implicitly(self) -> None:
        sandbox = _InstallerSandbox(self)
        completed = sandbox.install()
        self._assert_refused_before_mutation(sandbox, completed)
        self.assertIn(f"refusing to re-enable disabled {LABEL} implicitly", completed.stderr)
        self.assertIn("--confirm-operator-disabled", completed.stderr)
        self.assertNotIn("enable", {command.split()[0] for command in sandbox.launchctl_commands()})

    def test_runnable_enabled_label_reaches_bootstrap_without_enable(self) -> None:
        sandbox = _InstallerSandbox(self)
        fake = sandbox.root / "bin" / "launchctl"
        script = fake.read_text(encoding="utf-8")
        self.assertIn(f'"{LABEL}" => disabled', script)
        fake.write_text(script.replace(f'"{LABEL}" => disabled', f'"{LABEL}" => enabled'), encoding="utf-8")
        completed = sandbox.install()
        commands = sandbox.launchctl_commands()
        verbs = [command.split()[0] for command in commands]
        self.assertIn("bootstrap", verbs, completed.stderr)
        self.assertNotIn("enable", verbs)
        self.assertIn("rollback verified", completed.stderr, "the fake bootstrap fails and rolls back")
        self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)


    def test_native_only_disable_passes_the_preflight_but_still_needs_the_explicit_flag(self) -> None:
        # The sandbox root is declared supervised and bound to a disposable
        # label; the fake launchd reports it (and the worker label) disabled
        # and not loaded, with the exact not-found pair: OPERATOR_DISABLED.
        label = disposable_label()
        sandbox = _InstallerSandbox(self)
        (sandbox.root / SENTINEL_NAME).write_text("", encoding="utf-8")
        (sandbox.root / "bin" / "launchctl").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
            "case \"$1\" in\n"
            "  print) printf 'Could not find service \"%s\" in domain for user gui: %s\\n' "
            "\"${2##*/}\" \"$(id -u)\" >&2; exit 113 ;;\n"
            "  print-disabled) printf '%s\\n' 'disabled services = {' "
            f"'\t\"{LABEL}\" => disabled' '\t\"{label}\" => disabled' '}}'; exit 0 ;;\n"
            "  bootstrap) exit 42 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        bound = {"DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT": str(sandbox.runtime_root),
                 "DISCLOSURE_WORKER_LAUNCHD_LABEL": label}
        refused = sandbox.install(extra=bound)
        self._assert_refused_before_mutation(sandbox, refused)
        self.assertIn(f"refusing to re-enable disabled {LABEL} implicitly", refused.stderr)
        self.assertIn(f"print-disabled gui/{os.getuid()}", sandbox.launchctl_commands())
        self.assertIn(f"print gui/{os.getuid()}/{label}", sandbox.launchctl_commands(),
                      "the preflight read the bound disposable label, never inventing a fault")
        confirmed = sandbox.install(extra=bound, arguments=("--confirm-operator-disabled",))
        verbs = [command.split()[0] for command in sandbox.launchctl_commands()]
        self.assertEqual(verbs.count("enable"), 1)
        self.assertLess(verbs.index("enable"), verbs.index("bootstrap"), "enable only with the flag, then bootstrap")
        self.assertIn("rollback verified", confirmed.stderr)
        self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)


class RestartStopGuardTests(unittest.TestCase):
    def test_worker_restart_refuses_a_recorded_stop_before_kickstart(self) -> None:
        sandbox = _InstallerSandbox(self)
        sha = _record_stop(sandbox)
        (sandbox.environment_directory / "worker.env").write_text(
            "".join(f"{name}={value}\n" for name, value in _sandbox_roots(sandbox).items()),
            encoding="utf-8",
        )
        make = shutil.which("make", path=sandbox.environment["PATH"])
        if make is None:
            self.skipTest("make is not installed")
        self.assertEqual(shutil.which("launchctl", path=sandbox.environment["PATH"]),
                         str(sandbox.root / "bin" / "launchctl"))
        completed = subprocess.run(
            [make, "--no-print-directory", "-C", str(_SERVICE_ROOT), "worker-restart",
             f"DISCLOSURE_ENV_DIR={sandbox.environment_directory}"],
            check=False, capture_output=True, env=sandbox.environment, text=True, timeout=120,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("worker control is not RUNNABLE", completed.stderr)
        self.assertIn(sha, completed.stdout + completed.stderr)
        self.assertFalse(sandbox.launchctl_log.exists() and sandbox.launchctl_commands(),
                         "no launchctl command (least of all kickstart) ran")
        self.assertTrue((sandbox.runtime_root / "control" / "worker-circuit-stop.json").exists())


if __name__ == "__main__":
    unittest.main()
