from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest

from scripts.mineru_ssh_tunnel import load_tunnel_config, ssh_command


class MinerUSshTunnelTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        identity = root / "tunnel-key"
        known_hosts = root / "known-hosts"
        config = root / "tunnel.env"
        identity.write_text("private", encoding="utf-8")
        known_hosts.write_text(
            "100.64.0.1 ssh-ed25519 "
            + base64.b64encode(b"host-key").decode()
            + "\n",
            encoding="utf-8",
        )
        config.write_text(
            "\n".join(
                (
                    "MINERU_SSH_HOST=100.64.0.1",
                    "MINERU_SSH_USER=help",
                    "MINERU_SSH_PORT=22",
                    f"MINERU_SSH_IDENTITY_FILE={identity}",
                    f"MINERU_SSH_KNOWN_HOSTS_FILE={known_hosts}",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        for path in (identity, known_hosts, config):
            path.chmod(0o600)
        return config

    def test_strict_config_builds_only_the_three_expected_forwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            values = load_tunnel_config(self._fixture(Path(tmp)))
            command = ssh_command(values)

        self.assertEqual(command.count("-L"), 3)
        self.assertIn("127.0.0.1:30002:127.0.0.1:30003", command)
        self.assertIn("127.0.0.1:30001:127.0.0.1:30001", command)
        self.assertIn("127.0.0.1:30004:127.0.0.1:9835", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("GlobalKnownHostsFile=/dev/null", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertNotIn("ClearAllForwardings=yes", command)

    def test_shell_syntax_and_unsafe_files_are_never_executed(self) -> None:
        for tamper in ("syntax", "mode", "extra", "duplicate"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                config = self._fixture(Path(tmp))
                if tamper == "syntax":
                    config.write_text(
                        config.read_text() + "MINERU_SSH_HOST=$(touch /tmp/no)\n"
                    )
                elif tamper == "mode":
                    config.chmod(0o644)
                elif tamper == "extra":
                    config.write_text(config.read_text() + "PATH=/tmp\n")
                else:
                    config.write_text(
                        config.read_text() + "MINERU_SSH_USER=other\n"
                    )
                with self.assertRaises((OSError, ValueError)):
                    load_tunnel_config(config)

    def test_known_hosts_must_be_one_exact_host_key(self) -> None:
        for line in (
            "@cert-authority * ssh-ed25519 AAAA\n",
            "other ssh-ed25519 AAAA\n",
            "100.64.0.1 ssh-rsa AAAA\n",
        ):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as tmp:
                config = self._fixture(Path(tmp))
                known_hosts = Path(tmp) / "known-hosts"
                known_hosts.write_text(line, encoding="utf-8")
                known_hosts.chmod(0o600)
                with self.assertRaises(ValueError):
                    load_tunnel_config(config)


if __name__ == "__main__":
    unittest.main()
