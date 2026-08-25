from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallLaunchdTests(unittest.TestCase):
    def test_bootstrap_failure_restores_plist_disabled_and_unloaded_state(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        installer = repository / "scripts" / "install_launchd.sh"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist = launch_agents / "com.agentinvest.disclosure-worker.plist"
            original = b"original-worker-plist\n"
            plist.write_bytes(original)

            environment_directory = root / "env"
            environment_directory.mkdir()
            runtime_root = root / "runtime"
            (environment_directory / "worker.env").write_text(
                "\n".join(
                    (
                        f"DISCLOSURE_DATA_ROOT={root / 'data'}",
                        f"DISCLOSURE_SHARED_ROOT={root / 'shared'}",
                        f"DISCLOSURE_RUNTIME_ROOT={runtime_root}",
                        f"MINERU_MODEL_CACHE={root / 'models'}",
                        f"HF_HOME={root / 'hf'}",
                        f"MODELSCOPE_CACHE={root / 'modelscope'}",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            (environment_directory / "cninfo.env").write_text("", encoding="utf-8")

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
                "'\"com.agentinvest.disclosure-worker\" => disabled'; exit 0 ;;\n"
                "  bootstrap) printf '%s\\n' loaded > \"$LAUNCHCTL_STATE\"; exit 42 ;;\n"
                "  bootout) : > \"$LAUNCHCTL_STATE\"; exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "DISCLOSURE_ENV_DIR": str(environment_directory),
                    "HOME": str(home),
                    "LAUNCHCTL_LOG": str(launchctl_log),
                    "LAUNCHCTL_STATE": str(launchctl_state),
                    "PATH": f"{fake_bin}:{environment['PATH']}",
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
            self.assertEqual(plist.read_bytes(), original)
            commands = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(line.startswith("bootstrap ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("bootout ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("enable ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("disable ") for line in commands), 1)
            self.assertFalse(any(line.startswith("kickstart ") for line in commands))
            self.assertEqual([path.name for path in launch_agents.iterdir()], [plist.name])
            self.assertEqual(launchctl_state.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
