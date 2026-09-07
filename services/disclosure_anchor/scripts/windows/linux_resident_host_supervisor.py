"""Default-off Linux stdlib supervisor: one fixed-source child, exact wait4 CPU.

Launch only in a fresh, single-threaded interpreter. No per-sample descendants,
network, filesystem writes, or service control. A normal close receipt includes
the child's full exit CPU and this supervisor's explicitly pre-attestation CPU.
It is not proof of Docker container absence; the external owner verifies that.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import math
import os
from pathlib import Path
import select
import signal
import time
import traceback
from types import ModuleType
from typing import Any


VERSION = "mineru.linux-resident-supervisor.v1"
MAX_SOURCE_BYTES = 65536
CLEANUP_GRACE_SECONDS = 2.0


def source_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(MAX_SOURCE_BYTES + 1)
    if len(value) > MAX_SOURCE_BYTES:
        raise ValueError("source exceeded bound")
    return value


def sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_sampler(source: bytes, expected_sha256: str) -> ModuleType:
    if len(source) > MAX_SOURCE_BYTES or sha256(source) != expected_sha256:
        raise ValueError("sampler source identity mismatch")
    module = ModuleType("_fixed_linux_host_sampler")
    exec(compile(source, "<fixed-linux-host-sampler>", "exec"), module.__dict__)
    return module


def read_frame(fd: int, maximum: int, deadline: float) -> bytes | None:
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError("relay input deadline")
        try:
            data = os.read(fd, maximum + 1 - len(buffer))
        except BlockingIOError:
            continue
        if not data:
            if buffer:
                raise ValueError("partial frame at EOF")
            return None
        buffer.extend(data)
        if len(buffer) > maximum:
            raise ValueError("relay frame exceeded bound")
        if b"\n" in buffer:
            payload, _, tail = buffer.partition(b"\n")
            if tail:
                raise ValueError("pipelined relay frames forbidden")
            return bytes(payload)


def reap(pid: int, deadline: float) -> tuple[int, Any]:
    while True:
        observed, status, usage = os.wait4(pid, os.WNOHANG)
        if observed:
            if observed != pid:
                raise RuntimeError("unexpected wait4 child identity")
            return status, usage
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("child reap deadline")
        time.sleep(min(0.01, remaining))


def terminate_and_reap(pid: int) -> None:
    # Only the unreaped, directly created child is a valid signal target.
    observed, _, _ = os.wait4(pid, os.WNOHANG)
    if observed == pid:
        return
    os.kill(pid, signal.SIGTERM)
    try:
        reap(pid, time.monotonic() + 0.25)
    except TimeoutError:
        os.kill(pid, signal.SIGKILL)
        reap(pid, time.monotonic() + 0.75)


def ensure_single_thread(sampler: ModuleType) -> None:
    status = sampler.read_kernel_file(Path("/proc") / str(os.getpid()) / "status")
    if [line.split() for line in status.splitlines() if line.startswith("Threads:")] != [["Threads:", "1"]]:
        raise ValueError("single-threaded supervisor required")


def run(config: dict[str, Any], sampler: ModuleType, sampler_sha256: str, supervisor_sha256: str) -> None:
    lease = sampler.integer(config["lease_ms"], minimum=1000) / 1000
    lifetime = sampler.integer(config["lifetime_ms"], minimum=1000) / 1000
    if lease > 30 or lifetime > 7200 or lease > lifetime:
        raise ValueError("lease/lifetime exceeds bound")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    started = time.monotonic()
    hard_end = started + lifetime
    deadline = min(hard_end, started + lease)
    # Operational expiry must enter finally/reap before the native-hang fuse.
    # This grace never extends the sampling lifetime or KPI denominator.
    signal.setitimer(signal.ITIMER_REAL, deadline - started + CLEANUP_GRACE_SECONDS)
    ensure_single_thread(sampler)
    own_identity = {
        "pid": os.getpid(), "start_ticks": sampler.self_process_stat()["start_ticks"],
        "source_sha256": supervisor_sha256, "sampler_source_sha256": sampler_sha256,
        "boot_id": config["boot_id"], "namespaces": sampler.host_namespaces(),
    }
    epoch = sampler.digest(own_identity)
    command_read, command_write = os.pipe()
    try:
        response_read, response_write = os.pipe()
    except BaseException:
        os.close(command_read)
        os.close(command_write)
        raise
    try:
        pid = os.fork()
    except BaseException:
        for fd in (command_read, command_write, response_read, response_write):
            os.close(fd)
        raise
    if pid == 0:
        try:
            os.close(command_write)
            os.close(response_read)
            os.dup2(command_read, 0)
            os.dup2(response_write, 1)
            os.close(command_read)
            os.close(response_write)
            sampler.run(config, sampler_sha256)
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    os.close(command_read)
    os.close(response_write)
    reaped = False
    try:
        for fd in (0, 1, command_write, response_read):
            os.set_blocking(fd, False)
        body = read_frame(response_read, sampler.MAX_FILE_BYTES, deadline)
        if body is None:
            raise ValueError("child exited before READY")
        ready = sampler.decode(body)
        identity = ready.get("identity")
        if (ready.get("contract_version") != sampler.VERSION or ready.get("kind") != "ready"
                or not isinstance(identity, dict) or identity.get("pid") != pid
                or identity.get("source_sha256") != sampler_sha256
                or identity.get("boot_id") != config["boot_id"]
                or ready.get("epoch_sha256") != sampler.digest(identity)):
            raise ValueError("child READY identity mismatch")
        sampler.emit({"contract_version": VERSION, "kind": "ready", "identity": own_identity,
                      "epoch_sha256": epoch, "sampler_ready": ready}, deadline=deadline)
        sequence = 0
        while True:
            command_bytes = read_frame(0, sampler.MAX_COMMAND_BYTES, deadline)
            if command_bytes is None:
                return  # Unexpected EOF: cleanup, but no successful receipt.
            command = sampler.decode(command_bytes)
            if (set(command) != {"command", "sequence"} or command["command"] not in {"sample", "close"}
                    or sampler.integer(command["sequence"], minimum=1) != sequence + 1):
                raise ValueError("invalid relay command/sequence")
            sequence += 1
            deadline = min(hard_end, time.monotonic() + lease)
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - time.monotonic()) + CLEANUP_GRACE_SECONDS)
            sampler.emit(command, fd=command_write, deadline=deadline)
            body = read_frame(response_read, sampler.MAX_FILE_BYTES, deadline)
            if body is None:
                raise ValueError("child exited without response")
            response = sampler.decode(body)
            if (response.get("contract_version") != sampler.VERSION or response.get("kind") != command["command"]
                    or response.get("sequence") != sequence or response.get("epoch_sha256") != ready["epoch_sha256"]):
                raise ValueError("child response identity/sequence mismatch")
            sampler.emit(response, deadline=deadline)
            if command["command"] == "close":
                if read_frame(response_read, sampler.MAX_FILE_BYTES, deadline) is not None:
                    raise ValueError("unexpected child frame after close")
                status, usage = reap(pid, deadline)
                reaped = True
                if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
                    raise ValueError("abnormal child exit; CPU receipt unavailable")
                cpu = {"user_ns_total": math.ceil(usage.ru_utime * 1_000_000_000),
                       "system_ns_total": math.ceil(usage.ru_stime * 1_000_000_000)}
                sampler.emit({
                    "contract_version": VERSION, "kind": "closed", "epoch_sha256": epoch,
                    "sequence": sequence, "sampler_epoch_sha256": ready["epoch_sha256"],
                    "sampler_pid": pid, "sampler_wait_status": status, "sampler_exit_cpu": cpu,
                    "supervisor_pre_attestation_cpu": sampler.self_cpu(),
                    "pre_attestation_monotonic_ns": time.monotonic_ns(),
                }, deadline=deadline)
                return
    finally:
        os.close(command_write)
        os.close(response_read)
        if not reaped:
            terminate_and_reap(pid)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-base64", required=True)
    parser.add_argument("--sampler-source-base64", required=True)
    parser.add_argument("--sampler-sha256", required=True)
    args = parser.parse_args()
    if len(args.config_base64) > 12000 or len(args.sampler_source_base64) > 90000:
        raise ValueError("encoded input exceeded bound")
    source = base64.b64decode(args.sampler_source_base64, validate=True)
    sampler = load_sampler(source, args.sampler_sha256)
    config = sampler.decode(base64.b64decode(args.config_base64, validate=True))
    own_source = globals().get("_resident_source_bytes")
    if own_source is None:
        own_source = source_bytes(Path(__file__))
    run(config, sampler, args.sampler_sha256, sha256(own_source))


if __name__ == "__main__":
    main()
