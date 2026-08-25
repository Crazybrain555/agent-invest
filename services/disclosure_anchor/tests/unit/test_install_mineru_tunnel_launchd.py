from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallMineruTunnelLaunchdTests(unittest.TestCase):
    def test_partial_bootstrap_restores_files_disabled_and_unloaded_state(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        installer = repository / "scripts" / "install_mineru_tunnel_launchd.sh"
        label = "com.agentinvest.mineru-tunnel"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            launch_agents = home / "Library" / "LaunchAgents"
            runtime = home / ".local" / "lib" / "agent-invest" / "mineru-tunnel"
            config_dir = home / ".config" / "agent-invest" / "disclosure_anchor"
            launch_agents.mkdir(parents=True)
            runtime.mkdir(parents=True)
            config_dir.mkdir(parents=True)

            plist = launch_agents / f"{label}.plist"
            wrapper = runtime / "mineru_ssh_tunnel.py"
            plist_original = b"original-tunnel-plist\n"
            wrapper_original = b"original-tunnel-wrapper\n"
            plist.write_bytes(plist_original)
            wrapper.write_bytes(wrapper_original)
            wrapper.chmod(0o600)

            identity = config_dir / "identity"
            known_hosts = config_dir / "known_hosts"
            tunnel_env = config_dir / "mineru-tunnel.env"
            identity.write_bytes(b"test-identity\n")
            known_hosts.write_text(
                "gpu-host ssh-ed25519 dGVzdA==\n",
                encoding="utf-8",
            )
            tunnel_env.write_text(
                "\n".join(
                    (
                        "MINERU_SSH_HOST=gpu-host",
                        "MINERU_SSH_USER=tunnel-user",
                        "MINERU_SSH_PORT=22",
                        f"MINERU_SSH_IDENTITY_FILE={identity}",
                        f"MINERU_SSH_KNOWN_HOSTS_FILE={known_hosts}",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            for private_file in (identity, known_hosts, tunnel_env):
                private_file.chmod(0o600)

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
                "'\"com.agentinvest.mineru-tunnel\" => disabled'; exit 0 ;;\n"
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
            self.assertEqual(plist.read_bytes(), plist_original)
            self.assertEqual(wrapper.read_bytes(), wrapper_original)
            commands = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(line.startswith("bootstrap ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("bootout ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("enable ") for line in commands), 1)
            self.assertEqual(sum(line.startswith("disable ") for line in commands), 1)
            self.assertFalse(any(line.startswith("kickstart ") for line in commands))
            self.assertEqual(launchctl_state.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
