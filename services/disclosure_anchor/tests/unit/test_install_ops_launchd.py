from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


_LABELS = (
    "com.agentinvest.postgres",
    "com.agentinvest.disclosure-doctor",
    "com.agentinvest.disclosure-gc",
)


class InstallOpsLaunchdTests(unittest.TestCase):
    def test_partial_bootstrap_restores_disabled_and_unloaded_state(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        installer = repository / "scripts" / "install_ops_launchd.sh"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            original: dict[Path, bytes] = {}
            for index, label in enumerate(_LABELS):
                path = launch_agents / f"{label}.plist"
                payload = f"original-{index}\n".encode()
                path.write_bytes(payload)
                original[path] = payload

            fake_bin = root / "bin"
            fake_bin.mkdir()
            launchctl_log = root / "launchctl.log"
            launchctl_state = root / "launchctl.state"
            launchctl_state.write_text("", encoding="utf-8")
            launchctl = fake_bin / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
                "case \"$1\" in\n"
                "  print) test -s \"$LAUNCHCTL_STATE\" ;;\n"
                "  print-disabled) printf '%s\\n' "
                "'\"com.agentinvest.disclosure-gc\" => disabled'; exit 0 ;;\n"
                "  bootstrap) printf '%s\\n' loaded > \"$LAUNCHCTL_STATE\"; exit 42 ;;\n"
                "  bootout) : > \"$LAUNCHCTL_STATE\"; exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "LAUNCHCTL_LOG": str(launchctl_log),
                    "LAUNCHCTL_STATE": str(launchctl_state),
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "TMPDIR": str(root),
                }
            )

            completed = subprocess.run(
                ["/bin/zsh", str(installer)],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("rollback verified", completed.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in original},
                original,
            )
            commands = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(line.startswith("bootstrap ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("bootout ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("enable ") for line in commands), 0)
            self.assertEqual(sum(line.startswith("disable ") for line in commands), 1)
            self.assertEqual(launchctl_state.read_text(encoding="utf-8"), "")
            self.assertEqual(list(root.glob("disclosure-ops-launchd.*")), [])

    def test_render_failure_never_mutates_unsnapshotted_formal_state(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        installer = repository / "scripts" / "install_ops_launchd.sh"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            original: dict[Path, bytes] = {}
            for index, label in enumerate(_LABELS):
                payload = f"original-{index}\n".encode()
                path = launch_agents / f"{label}.plist"
                path.write_bytes(payload)
                original[path] = payload

            fake_bin = root / "bin"
            fake_bin.mkdir()
            launchctl_log = root / "launchctl.log"
            launchctl = fake_bin / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
                "case \"$1\" in\n"
                "  print) exit 1 ;;\n"
                "  print-disabled) printf '%s\\n' "
                "'\"com.agentinvest.disclosure-gc\" => disabled'; exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            failing_sed = fake_bin / "sed"
            failing_sed.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
            failing_sed.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "LAUNCHCTL_LOG": str(launchctl_log),
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "TMPDIR": str(root),
                }
            )
            completed = subprocess.run(
                ["/bin/zsh", str(installer)],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("before formal mutation", completed.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in original},
                original,
            )
            commands = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(line.startswith("print ") for line in commands), 3)
            self.assertEqual(
                sum(line.startswith("print-disabled ") for line in commands),
                1,
            )
            self.assertFalse(
                any(
                    line.startswith(("bootout ", "enable ", "disable ", "bootstrap "))
                    for line in commands
                ),
                commands,
            )
            self.assertEqual(list(root.glob("disclosure-ops-launchd.*")), [])


if __name__ == "__main__":
    unittest.main()
