"""Independent acceptance: the installer's deployment preflight precedes every mutation.

Pro §C5/§C6 and root's mandatory acceptance: an installer preflight failure causes zero
enable/bootstrap and leaves the plist unchanged; the public/invalid stop gate stays first; the
root-verified prepared-key lifetime is threaded unchanged to the preflight, and never defaulted.
The byte-exact installer runs in the existing disposable sandbox (fake launchctl/pgrep, rebuilt
environment); only its preflight child is a recording stub, except in the real-preflight case.
"""

from __future__ import annotations

import os
import subprocess
import unittest

from tests.unit.test_f5_installer_stop_guard_independent import _record_stop
from tests.unit.test_install_launchd import LABEL, PREFLIGHT_MODULE_ARGS, READ_ONLY_LAUNCHCTL, _InstallerSandbox


_REFUSAL = "refusing to install: the worker deployment preflight is not ready"
_PREFLIGHT = " ".join((*PREFLIGHT_MODULE_ARGS, "--format", "terminal"))


class InstallerPreflightTests(unittest.TestCase):
    def _assert_nothing_mutated(self, sandbox: _InstallerSandbox,
                                completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)
        self.assertEqual([path.name for path in sandbox.launch_agents.iterdir()], [sandbox.plist.name])
        verbs = [command.split()[0] for command in sandbox.launchctl_commands()]
        self.assertLessEqual(set(verbs), READ_ONLY_LAUNCHCTL, completed.stderr)
        self.assertNotIn("enable", verbs)
        self.assertNotIn("bootstrap", verbs)
        self.assertFalse(sandbox.logs.exists(), "refused before the launchd log directory")
        self.assertNotIn("rollback", completed.stderr)

    def test_a_not_ready_preflight_refuses_with_78_before_any_plist_or_launchd_mutation(self) -> None:
        for arguments in ((), ("--confirm-operator-disabled",)):
            with self.subTest(arguments=arguments):
                sandbox = _InstallerSandbox(self, preflight="not-ready")
                completed = sandbox.install(arguments=arguments)
                self.assertEqual(completed.returncode, 78, completed.stderr)
                self.assertIn(_REFUSAL, completed.stderr)
                self.assertIn("BLOCKER", completed.stdout)
                self._assert_nothing_mutated(sandbox, completed)
                self.assertEqual(sandbox.preflight_commands(), [_PREFLIGHT])
                # Only the loaded-job probe ran; the disabled-state read comes after the preflight.
                self.assertEqual(sandbox.launchctl_commands(), [f"print gui/{os.getuid()}/{LABEL}"])

    def test_the_actual_preflight_refuses_an_unqualified_install(self) -> None:
        # No stub: the real ``worker deployment-preflight`` runs over the sandbox settings, which
        # carry no qualification evidence, and the installer honours its not-ready exit.
        for mode, keyring in (("legacy-sync", False), ("staged-v4", True)):
            with self.subTest(mode=mode):
                sandbox = _InstallerSandbox(self, preflight="real")
                completed = sandbox.install(
                    mode=mode, keyring=sandbox.keyring() if keyring else None,
                    arguments=("--confirm-operator-disabled",),
                )
                self.assertEqual(completed.returncode, 78, completed.stderr)
                self.assertIn(_REFUSAL, completed.stderr)
                self.assertIn("ready_to_install=false", completed.stdout)
                self.assertIn("BLOCKER deployment qualification", completed.stdout)
                self._assert_nothing_mutated(sandbox, completed)
                self.assertEqual(sandbox.preflight_commands(), [], "the real preflight ran, not the stub")

    def test_a_ready_preflight_hands_over_to_the_unchanged_launchd_sequence(self) -> None:
        sandbox = _InstallerSandbox(self)
        completed = sandbox.install(arguments=("--confirm-operator-disabled",))
        self.assertEqual(sandbox.preflight_commands(), [_PREFLIGHT])
        verbs = [command.split()[0] for command in sandbox.launchctl_commands()]
        self.assertEqual((verbs.count("enable"), verbs.count("bootstrap")), (1, 1), completed.stderr)
        self.assertIn("rollback verified", completed.stderr, "the fake bootstrap fails and rolls back")
        self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)

    def test_the_prepared_key_lifetime_reaches_the_preflight_unchanged(self) -> None:
        for arguments, expected in (
            (("--prepared-key-ttl-seconds", "86400"), f"{_PREFLIGHT} --prepared-key-ttl-seconds 86400"),
            (("--prepared-key-ttl-seconds", "86400", "--confirm-operator-disabled"),
             f"{_PREFLIGHT} --prepared-key-ttl-seconds 86400"),
            (("--confirm-operator-disabled", "--prepared-key-ttl-seconds", "1"),
             f"{_PREFLIGHT} --prepared-key-ttl-seconds 1"),
            ((), _PREFLIGHT),
        ):
            with self.subTest(arguments=arguments):
                sandbox = _InstallerSandbox(self, preflight="not-ready")
                completed = sandbox.install(arguments=arguments)
                self.assertEqual(completed.returncode, 78, completed.stderr)
                self.assertEqual(sandbox.preflight_commands(), [expected])

    def test_an_invalid_key_lifetime_is_a_usage_error_before_anything_runs(self) -> None:
        for arguments in (
            ("--prepared-key-ttl-seconds",),
            ("--prepared-key-ttl-seconds", "0"),
            ("--prepared-key-ttl-seconds", "-86400"),
            ("--prepared-key-ttl-seconds", "86400s"),
            ("--prepared-key-ttl-seconds", "8.64e4"),
            ("--prepared-key-ttl-seconds", ""),
            ("--prepared-key-ttl-seconds", "086400"),
            ("--prepared-key-ttl-seconds", "--confirm-operator-disabled"),
            ("--unknown",),
        ):
            with self.subTest(arguments=arguments):
                sandbox = _InstallerSandbox(self)
                completed = sandbox.install(arguments=arguments)
                self.assertEqual(completed.returncode, 64, completed.stderr)
                self.assertIn("usage:", completed.stderr)
                self.assertEqual(sandbox.launchctl_commands(), [])
                self.assertEqual(sandbox.preflight_commands(), [])
                self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)

    def test_a_public_or_invalid_stop_refuses_before_the_preflight_can_read_the_database(self) -> None:
        cases = ("PUBLIC_STOP", "INVALID_STOP", "CONTROL_UNAVAILABLE")
        for state in cases:
            with self.subTest(state=state):
                sandbox = _InstallerSandbox(self)
                if state == "PUBLIC_STOP":
                    _record_stop(sandbox)
                elif state == "INVALID_STOP":
                    control = sandbox.runtime_root / "control"
                    control.mkdir(mode=0o700)
                    (control / "worker-circuit-stop.json").write_bytes(b"{truncated")
                    (control / "worker-circuit-stop.json").chmod(0o600)
                else:
                    sandbox.runtime_root.rmdir()
                completed = sandbox.install(arguments=("--confirm-operator-disabled", "--prepared-key-ttl-seconds", "86400"))
                self.assertEqual(completed.returncode, 78, completed.stderr)
                self.assertIn(state, completed.stderr)
                self.assertNotIn(_REFUSAL, completed.stderr)
                self.assertEqual(sandbox.preflight_commands(), [], "the stop gate precedes the preflight")
                self._assert_nothing_mutated(sandbox, completed)


if __name__ == "__main__":
    unittest.main()
