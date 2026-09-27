"""Direct runs of scripts/install_launchd.sh inside a disposable sandbox.

A byte-exact copy of the installer runs from a sandbox repository, because
the installer derives its repository from its own path. The sandbox
repository links this tree's ``src`` and ``scripts/launchd``, and its
``.venv/bin/python`` executes the real virtualenv interpreter for every step
(Settings loader, keyring loader, stop gate, progress check) except one child:
the worker's read-only ``deployment-preflight``, whose own contract is tested
with the preflight itself. That child is recorded and answered with the
sandbox's configured readiness ("ready" by default, "not-ready", or "real" to
run the actual preflight). Fake launchctl and pgrep lead a PATH built from
scratch, and the child environment is rebuilt rather than inherited, so no
real launchd job, private env directory, keyring or operator shell variable
takes part.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from disclosure_anchor.adapters.security.provider_secret_keyring import (
    PROVIDER_SECRET_KEYRING_FORMAT,
    load_provider_secret_keyring_file,
)
from disclosure_anchor.application.ports.provider_secret_cipher_v4 import (
    ProviderSecretKeyringInvalid,
)


SERVICE_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = SERVICE_ROOT / "scripts" / "install_launchd.sh"
# Not resolved: the virtualenv interpreter is identified by its own path.
VENV_PYTHON = SERVICE_ROOT / ".venv" / "bin" / "python"
PREFLIGHT_MODULE_ARGS = ("-m", "disclosure_anchor.cli.worker", "deployment-preflight")
LABEL = "com.agentinvest.disclosure-worker"
KEK_ID = "kek-installer"
KEK_HEX = "5c" * 32
READ_ONLY_LAUNCHCTL = frozenset({"print", "print-disabled"})
# The fake label starts natively disabled; re-enabling it is an explicit
# operator confirmation that the disable was ordinary maintenance.
CONFIRM_OPERATOR_DISABLED = ("--confirm-operator-disabled",)
FAKE_LAUNCHCTL = (
    "#!/bin/sh\n"
    "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
    "case \"$1\" in\n"
    "  print) test -s \"$LAUNCHCTL_STATE\" ;;\n"
    "  print-disabled) printf '%s\\n' 'disabled services = {' "
    f"'\t\"{LABEL}\" => disabled' '}}'; exit 0 ;;\n"
    "  bootstrap) printf '%s\\n' loaded > \"$LAUNCHCTL_STATE\"; exit 42 ;;\n"
    "  bootout) : > \"$LAUNCHCTL_STATE\"; exit 0 ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)


PYTHON_SHIM = (
    "#!/bin/sh\n"
    "if [ \"$1\" = -m ] && [ \"$2\" = disclosure_anchor.cli.worker ] "
    "&& [ \"$3\" = deployment-preflight ] && [ \"$PREFLIGHT_MODE\" != real ]; then\n"
    "  printf '%s\\n' \"$*\" >> \"$PREFLIGHT_LOG\"\n"
    "  if [ \"$PREFLIGHT_MODE\" = ready ]; then\n"
    "    echo 'deployment preflight: ready_to_install=true (sandbox stub)'; exit 0\n"
    "  fi\n"
    "  echo 'deployment preflight: ready_to_install=false (sandbox stub)'\n"
    "  echo '  BLOCKER sandbox stub is not ready'; exit 78\n"
    "fi\n"
    "exec __VENV_PYTHON__ \"$@\"\n"
)


def _keyring_text(*, format_name: str = PROVIDER_SECRET_KEYRING_FORMAT) -> str:
    return json.dumps(
        {"format": format_name, "primary_kek_id": KEK_ID, "keks": {KEK_ID: KEK_HEX}}
    )


class _InstallerSandbox:
    """One disposable HOME, env directory and fake launchd per installer run."""

    def __init__(self, test: unittest.TestCase, *, preflight: str = "ready") -> None:
        if preflight not in {"ready", "not-ready", "real"}:
            raise ValueError(f"unknown sandbox preflight mode {preflight!r}")
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        self.installer = self.repo / "scripts" / "install_launchd.sh"
        shutil.copyfile(INSTALLER, self.installer)
        test.assertEqual(self.installer.read_bytes(), INSTALLER.read_bytes())
        (self.repo / "src").symlink_to(SERVICE_ROOT / "src", target_is_directory=True)
        (self.repo / "scripts" / "launchd").symlink_to(SERVICE_ROOT / "scripts" / "launchd", target_is_directory=True)
        shim = self.repo / ".venv" / "bin" / "python"
        shim.parent.mkdir(parents=True)
        shim.write_text(PYTHON_SHIM.replace("__VENV_PYTHON__", shlex.quote(str(VENV_PYTHON))), encoding="utf-8")
        shim.chmod(0o755)
        self.preflight_log = self.root / "preflight.log"
        home = self.root / "home"
        self.logs = home / "Library" / "Logs"
        self.launch_agents = home / "Library" / "LaunchAgents"
        self.launch_agents.mkdir(parents=True)
        self.plist = self.launch_agents / f"{LABEL}.plist"
        self.original_plist = b"original-worker-plist\n"
        self.plist.write_bytes(self.original_plist)
        self.environment_directory = self.root / "env"
        self.environment_directory.mkdir()
        (self.environment_directory / "cninfo.env").write_text("", encoding="utf-8")
        self.launchctl_log = self.root / "launchctl.log"
        self.launchctl_state = self.root / "launchctl.state"
        self.launchctl_state.write_text("", encoding="utf-8")
        # A trusted (0700, own uid) runtime root, as on the production volume:
        # the worker start gate treats a missing root as unverifiable control
        # storage and refuses, and this root never owns the launchd label.
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir(mode=0o700)
        self.runtime_root.chmod(0o700)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        for name, script in (
            ("launchctl", FAKE_LAUNCHCTL),
            ("pgrep", "#!/bin/sh\nexit 1\n"),
        ):
            fake = fake_bin / name
            fake.write_text(script, encoding="utf-8")
            fake.chmod(0o755)
        # The preflight reads the process environment, so an operator-exported
        # WORKER_* or DISCLOSURE_* value must not decide the outcome.
        self.environment = {
            "DISCLOSURE_ENV_DIR": str(self.environment_directory),
            "HOME": str(home),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            "LAUNCHCTL_STATE": str(self.launchctl_state),
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PREFLIGHT_LOG": str(self.preflight_log),
            "PREFLIGHT_MODE": preflight,
            # The tree under test is never written, not even bytecode.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # Refuse to run at all if the real launchctl could be reached.
        for name in ("launchctl", "pgrep"):
            test.assertEqual(
                shutil.which(name, path=self.environment["PATH"]),
                str(fake_bin / name),
            )

    def keyring(self, *, text: str | None = None, mode: int = 0o600) -> Path:
        path = self.root / "keyring.json"
        path.write_text(_keyring_text() if text is None else text, encoding="utf-8")
        path.chmod(mode)
        return path

    def install(
        self,
        *,
        mode: str | None = None,
        keyring: Path | None = None,
        arguments: tuple[str, ...] = (),
        extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        settings = {
            "DISCLOSURE_DATA_ROOT": self.root / "data",
            "DISCLOSURE_SHARED_ROOT": self.root / "shared",
            "DISCLOSURE_RUNTIME_ROOT": self.runtime_root,
            "MINERU_MODEL_CACHE": self.root / "models",
            "HF_HOME": self.root / "hf",
            "MODELSCOPE_CACHE": self.root / "modelscope",
        }
        if keyring is not None:
            settings["DISCLOSURE_V4_SECRET_KEYRING_FILE"] = keyring
        settings.update(extra or {})
        lines = [f"{name}={shlex.quote(str(value))}" for name, value in settings.items()]
        if mode is not None:
            lines.append(f"WORKER_PARSE_EXECUTION_MODE={mode}")
        (self.environment_directory / "worker.env").write_text(
            "\n".join((*lines, "")), encoding="utf-8"
        )
        return subprocess.run(
            ["/bin/zsh", str(self.installer), *arguments],
            check=False,
            capture_output=True,
            env=self.environment,
            text=True,
            timeout=120,
        )

    def launchctl_commands(self) -> list[str]:
        if not self.launchctl_log.exists():
            return []
        return self.launchctl_log.read_text(encoding="utf-8").splitlines()

    def preflight_commands(self) -> list[str]:
        """The recorded stub ``deployment-preflight`` invocations (argv after the interpreter)."""

        if not self.preflight_log.exists():
            return []
        return self.preflight_log.read_text(encoding="utf-8").splitlines()


class InstallLaunchdTests(unittest.TestCase):
    def test_bootstrap_failure_restores_plist_disabled_and_unloaded_state(self) -> None:
        sandbox = _InstallerSandbox(self)

        completed = sandbox.install(arguments=CONFIRM_OPERATOR_DISABLED)

        self._assert_bootstrap_rolled_back(sandbox, completed)

    def test_staged_v4_valid_keyring_passes_preflight_to_bootstrap(self) -> None:
        sandbox = _InstallerSandbox(self)
        keyring = sandbox.keyring()
        self.assertEqual(load_provider_secret_keyring_file(keyring).primary_kek_id(), KEK_ID)

        completed = sandbox.install(
            mode="staged-v4", keyring=keyring, arguments=CONFIRM_OPERATOR_DISABLED
        )

        self._assert_bootstrap_rolled_back(sandbox, completed)

    def test_legacy_sync_never_opens_a_configured_keyring(self) -> None:
        sandbox = _InstallerSandbox(self)
        rejected = sandbox.keyring(mode=0o644)
        with self.assertRaises(ProviderSecretKeyringInvalid):
            load_provider_secret_keyring_file(rejected)

        completed = sandbox.install(
            mode="legacy-sync", keyring=rejected, arguments=CONFIRM_OPERATOR_DISABLED
        )

        self._assert_bootstrap_rolled_back(sandbox, completed)

    def test_staged_v4_keyring_failure_stops_before_log_plist_or_launchd_mutation(
        self,
    ) -> None:
        cases = (
            ("pointer absent", lambda sandbox: None, "ProviderSecretKeyringUnavailable"),
            (
                "file absent",
                lambda sandbox: sandbox.root / "absent-keyring.json",
                "ProviderSecretKeyringInvalid",
            ),
            (
                "unreadable",
                lambda sandbox: sandbox.keyring(mode=0o000),
                "ProviderSecretKeyringInvalid",
            ),
            (
                "group readable",
                lambda sandbox: sandbox.keyring(mode=0o640),
                "ProviderSecretKeyringInvalid",
            ),
            (
                "unsupported format",
                lambda sandbox: sandbox.keyring(
                    text=_keyring_text(format_name="disclosure-v4-secret-keyring.v0")
                ),
                "ProviderSecretKeyringInvalid",
            ),
        )
        for case, configure_keyring, error in cases:
            with self.subTest(case=case):
                if case == "unreadable" and os.geteuid() == 0:
                    self.skipTest("root can open a mode-000 file")
                sandbox = _InstallerSandbox(self)

                completed = sandbox.install(
                    mode="staged-v4", keyring=configure_keyring(sandbox)
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(error, completed.stderr)
                self.assertNotIn("rollback", completed.stderr)
                self._assert_no_key_material(completed)
                self.assertFalse(sandbox.logs.exists())
                self.assertEqual(
                    [path.name for path in sandbox.launch_agents.iterdir()],
                    [sandbox.plist.name],
                )
                self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)
                commands = sandbox.launchctl_commands()
                self.assertEqual(commands[0], f"print gui/{os.getuid()}/{LABEL}")
                self.assertLessEqual(
                    {command.split()[0] for command in commands}, READ_ONLY_LAUNCHCTL
                )

    def _assert_bootstrap_rolled_back(
        self,
        sandbox: _InstallerSandbox,
        completed: subprocess.CompletedProcess[str],
    ) -> None:
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("rollback verified", completed.stderr)
        self.assertNotIn("ProviderSecret", completed.stderr)
        self._assert_no_key_material(completed)
        self.assertEqual(sandbox.plist.read_bytes(), sandbox.original_plist)
        commands = sandbox.launchctl_commands()
        self.assertEqual(sum(line.startswith("bootstrap ") for line in commands), 1)
        self.assertEqual(sum(line.startswith("bootout ") for line in commands), 1)
        self.assertEqual(sum(line.startswith("enable ") for line in commands), 1)
        self.assertEqual(sum(line.startswith("disable ") for line in commands), 1)
        self.assertFalse(any(line.startswith("kickstart ") for line in commands))
        self.assertEqual(
            [path.name for path in sandbox.launch_agents.iterdir()], [sandbox.plist.name]
        )
        self.assertEqual(sandbox.launchctl_state.read_text(encoding="utf-8"), "")

    def _assert_no_key_material(self, completed: subprocess.CompletedProcess[str]) -> None:
        output = completed.stdout + completed.stderr
        self.assertNotIn(KEK_HEX, output)
        self.assertNotIn(KEK_ID, output)


if __name__ == "__main__":
    unittest.main()
