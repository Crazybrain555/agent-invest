"""M6 line transport over the existing pinned native management SSH adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from disclosure_anchor.adapters.runtime.m6_owner_protocol import M6LineOwnerTransport
from disclosure_anchor.adapters.runtime.resident_ssh_http import (
    ResidentSSHConfig, _Session, _read_private_config,
)


def m6_ssh_owner_transport(
    *, config: ResidentSSHConfig, token_path: str, remote_port: int,
    continuous_ns: Callable[[], int], timeout_ns: int = 5_000_000_000,
) -> M6LineOwnerTransport:
    """Explicit account/port/key files; never fall back to a business SSH key.

    Reuses the existing component's owner-thread, private-file and exact-host-key
    checks. The optional reviewed Paramiko dependency is loaded on first use;
    no daemon, SSH configuration change or global dependency installation.
    """
    if not Path(token_path).is_absolute():
        raise ValueError("M6 private token path must be absolute")
    token = _read_private_config(token_path, secret=True).removesuffix("\n")
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("M6 private token file is not canonical")
    if type(remote_port) is not int or not 1024 <= remote_port <= 65535:
        raise ValueError("M6 loopback owner port invalid")
    session: _Session | None = None

    def open_channel(timeout: float) -> Any:
        nonlocal session
        if session is None:
            started = continuous_ns()
            session = _Session(config, remote_port=remote_port, timeout=timeout)
            finished = continuous_ns()
            if type(started) is not int or type(finished) is not int or finished < started:
                raise ValueError("M6 continuous clock regressed during SSH startup")
            remaining = timeout - (finished - started) / 1_000_000_000
            if remaining <= 0:
                raise TimeoutError("M6 pinned SSH startup exhausted the exchange deadline")
        else:
            remaining = timeout
        return session.open_channel(remaining)

    def close_session() -> None:
        nonlocal session
        if session is not None:
            session.close()
            session = None

    return M6LineOwnerTransport(token=token, open_channel=open_channel, close_session=close_session,
                               continuous_ns=continuous_ns, timeout_ns=timeout_ns)
