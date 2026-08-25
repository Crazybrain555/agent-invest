"""Exec the dedicated MinerU SSH forwards from a strict private key-value file."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import re
import stat
import sys


ALLOWED_KEYS = {
    "MINERU_SSH_HOST",
    "MINERU_SSH_USER",
    "MINERU_SSH_PORT",
    "MINERU_SSH_IDENTITY_FILE",
    "MINERU_SSH_KNOWN_HOSTS_FILE",
}
REQUIRED_KEYS = ALLOWED_KEYS - {"MINERU_SSH_PORT"}
VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
MAX_CONFIG_BYTES = 16 * 1024


def _private_regular_file(path: Path, *, label: str) -> None:
    # os.lstat keeps the wrapper compatible with macOS's system Python 3.9
    # while preserving the no-symlink check required by the tunnel contract.
    metadata = os.lstat(path)
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} must be an owner-only 0600 absolute regular file")


def _read_private_file(path: Path, *, label: str, limit: int) -> bytes:
    _private_regular_file(path, label=label)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        initial_metadata = os.fstat(descriptor)
        encoded = os.read(descriptor, limit + 1)
        final_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(encoded) > limit
        or initial_metadata.st_dev != final_metadata.st_dev
        or initial_metadata.st_ino != final_metadata.st_ino
        or initial_metadata.st_size != final_metadata.st_size
        or final_metadata.st_size != len(encoded)
        or initial_metadata.st_mtime_ns != final_metadata.st_mtime_ns
    ):
        raise ValueError(f"{label} is oversized or changed while being read")
    return encoded


def _validate_known_hosts(path: Path, *, expected_host: str) -> None:
    encoded = _read_private_file(path, label="known_hosts", limit=16 * 1024)
    try:
        lines = [line.strip() for line in encoded.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise ValueError("known_hosts must be UTF-8") from exc
    if len(lines) != 1:
        raise ValueError("known_hosts must contain exactly one pinned key")
    fields = lines[0].split()
    if len(fields) != 3 or fields[:2] != [expected_host, "ssh-ed25519"]:
        raise ValueError("known_hosts must pin the exact host with one Ed25519 key")
    try:
        key_blob = base64.b64decode(fields[2], validate=True)
    except ValueError as exc:
        raise ValueError("known_hosts public key is not canonical base64") from exc
    if not key_blob:
        raise ValueError("known_hosts public key is empty")


def load_tunnel_config(path: Path) -> dict[str, str]:
    encoded = _read_private_file(
        path, label="tunnel config", limit=MAX_CONFIG_BYTES
    )
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("tunnel config must be UTF-8") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if (
            not separator
            or name not in ALLOWED_KEYS
            or name in values
            or not value
            or VALUE_RE.fullmatch(value) is None
        ):
            raise ValueError(f"invalid tunnel config line {line_number}")
        values[name] = value
    if not REQUIRED_KEYS.issubset(values):
        raise ValueError("tunnel config is missing required keys")
    port = values.get("MINERU_SSH_PORT", "22")
    if port != "22":
        raise ValueError("MINERU_SSH_PORT must be the audited port 22")
    values["MINERU_SSH_PORT"] = port
    for key in ("MINERU_SSH_IDENTITY_FILE", "MINERU_SSH_KNOWN_HOSTS_FILE"):
        _private_regular_file(Path(values[key]), label=key)
    _validate_known_hosts(
        Path(values["MINERU_SSH_KNOWN_HOSTS_FILE"]),
        expected_host=values["MINERU_SSH_HOST"],
    )
    return values


def ssh_command(values: dict[str, str]) -> list[str]:
    return [
        "/usr/bin/ssh",
        "-N",
        "-T",
        "-F",
        "/dev/null",
        "-p",
        values["MINERU_SSH_PORT"],
        "-i",
        values["MINERU_SSH_IDENTITY_FILE"],
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={values['MINERU_SSH_KNOWN_HOSTS_FILE']}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "TCPKeepAlive=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "PermitLocalCommand=no",
        "-L",
        "127.0.0.1:30002:127.0.0.1:30003",
        "-L",
        "127.0.0.1:30001:127.0.0.1:30001",
        "-L",
        "127.0.0.1:30004:127.0.0.1:9835",
        "--",
        f"{values['MINERU_SSH_USER']}@{values['MINERU_SSH_HOST']}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mineru_ssh_tunnel")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home()
        / ".config"
        / "agent-invest"
        / "disclosure_anchor"
        / "mineru-tunnel.env",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = load_tunnel_config(args.env_file)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[abort] {exc}") from exc
    if args.check:
        print("MinerU tunnel config: PASS")
        return 0
    os.execv("/usr/bin/ssh", ssh_command(values))
    return 1


if __name__ == "__main__":
    sys.exit(main())
