"""Default-off stdlib-only, read-only cgroup-v2 sampler for a leased container.

One process, no descendants, network, writes, service control, or subprocesses.
The owner supplies observed identities, checks READY, and verifies container
absence after exit. A default-action SIGALRM also bounds blocked native IO.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import resource
import select
import signal
import time
from typing import Any


VERSION = "mineru.linux-resident-host.v1"
MAX_FILE_BYTES = 65536
MAX_COMMAND_BYTES = 1024


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload, object_pairs_hook=_pairs)
    if not isinstance(value, dict) or canonical(value) != payload:
        raise ValueError("noncanonical object")
    return value


def integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid integer")
    return value


def counters(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        key, value = line.split()
        if key in result or not value.isdecimal():
            raise ValueError("invalid or duplicate counter")
        result[key] = int(value)
    return result


def pressure(text: str) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        if kind not in {"some", "full"} or kind in rows:
            raise ValueError("invalid pressure kind")
        values = dict(field.split("=", 1) for field in fields)
        if len(values) != len(fields) or set(values) != {"avg10", "avg60", "avg300", "total"}:
            raise ValueError("invalid pressure fields")
        averages = [float(values[key]) for key in ("avg10", "avg60", "avg300")]
        if any(not math.isfinite(value) or not 0 <= value <= 100 for value in averages):
            raise ValueError("invalid pressure average")
        if not values["total"].isdecimal():
            raise ValueError("invalid pressure total")
        rows[kind] = dict(zip(("avg10_pct", "avg60_pct", "avg300_pct"), averages, strict=True))
        rows[kind]["total_stall_us"] = int(values["total"])
    return {
        "some": rows["some"], "full": rows.get("full"),
        "full_status": "supported" if "full" in rows else "unsupported",
        "full_reason": None if "full" in rows else "collector_unsupported",
    }


def proc_stat(text: str, pid: int) -> dict[str, int]:
    # comm can contain spaces and ')' characters; numeric fields start after
    # the final ')'. Linux stat fields are one-based, state is field 3.
    prefix, separator, tail = text.rpartition(") ")
    if not separator or not prefix.startswith(str(pid) + " ("):
        raise ValueError("invalid process stat identity")
    fields = tail.split()
    if len(fields) < 22 or fields[0] in {"Z", "X", "x"}:
        raise ValueError("process absent or terminal")
    return {
        "user_ticks": integer(int(fields[11])),
        "system_ticks": integer(int(fields[12])),
        "start_ticks": integer(int(fields[19]), minimum=1),
    }


def kib_fields(text: str, names: tuple[str, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        key = fields[0].removesuffix(":")
        if key not in names:
            continue
        if key in result or len(fields) != 3 or fields[2] != "kB" or not fields[1].isdecimal():
            raise ValueError("invalid memory field")
        result[key] = int(fields[1]) * 1024
    if set(result) != set(names):
        raise KeyError("required memory field missing")
    return result


class PinnedDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        value = os.fstat(self.fd)
        self.identity = (value.st_dev, value.st_ino)
        try:
            self.verify()
        except BaseException:
            os.close(self.fd)
            raise

    def verify(self) -> None:
        value = self.path.stat(follow_symlinks=False)
        if (value.st_dev, value.st_ino) != self.identity:
            raise ValueError("pinned directory identity drift")

    def read(self, name: str) -> str:
        if "/" in name or name in {".", ".."}:
            raise ValueError("invalid file component")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            result = bytearray()
            while len(result) <= MAX_FILE_BYTES:
                block = os.read(fd, min(8192, MAX_FILE_BYTES + 1 - len(result)))
                if not block:
                    return result.decode("ascii")
                result.extend(block)
            raise ValueError("kernel file exceeded bound")
        finally:
            os.close(fd)

    def close(self) -> None:
        os.close(self.fd)


class HostSampler:
    def __init__(self, config: dict[str, Any], *, proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")) -> None:
        if set(config) != {"boot_id", "members", "parent_device", "parent_inode", "lease_ms", "lifetime_ms"}:
            raise ValueError("invalid config shape")
        if not isinstance(config["boot_id"], str) or not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", config["boot_id"]):
            raise ValueError("invalid Linux boot identity")
        self.config = config
        self.proc_root = proc_root
        self.handles: list[PinnedDirectory] = []
        self.tick_hz = os.sysconf("SC_CLK_TCK")
        if self.tick_hz < 1:
            raise ValueError("invalid kernel tick frequency")
        self._last: dict[str, int] = {}
        try:
            self.proc = self._pin(proc_root)
            self.boot = self._pin(proc_root / "sys/kernel/random")
            members = config["members"]
            if not isinstance(members, dict) or set(members) != {"api", "inference", "proxy"}:
                raise ValueError("exact service member set required")
            paths: list[str] = []
            self.members: dict[str, PinnedDirectory] = {}
            pids: set[int] = set()
            for role, member in members.items():
                if not isinstance(member, dict) or set(member) != {"pid", "start_ticks", "cgroup"}:
                    raise ValueError("invalid member identity")
                pid = integer(member["pid"], minimum=1)
                integer(member["start_ticks"], minimum=1)
                path = member["cgroup"]
                if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_.:/-]+", path) or posixpath.normpath(path) != path or path == "/":
                    raise ValueError("invalid member cgroup")
                if pid in pids or path in paths:
                    raise ValueError("duplicate service member")
                pids.add(pid)
                paths.append(path)
                self.members[role] = self._pin(proc_root / str(pid))
            self.parent_path = posixpath.commonpath(paths)
            if self.parent_path == "/" or self.parent_path in paths:
                raise ValueError("non-root common service parent required")
            self.parent = self._pin(cgroup_root / self.parent_path.lstrip("/"))
            expected = (integer(config["parent_device"]), integer(config["parent_inode"], minimum=1))
            if self.parent.identity != expected:
                raise ValueError("common parent identity mismatch")
            self.verify()
            self.identity = {
                "boot_id": config["boot_id"], "members": members,
                "parent_path": self.parent_path, "parent_device": expected[0], "parent_inode": expected[1],
            }
        except BaseException:
            self.close()
            raise

    def _pin(self, path: Path) -> PinnedDirectory:
        handle = PinnedDirectory(path)
        self.handles.append(handle)
        return handle

    def verify(self) -> None:
        for handle in self.handles:
            handle.verify()
        if self.boot.read("boot_id").strip() != self.config["boot_id"]:
            raise ValueError("Linux boot identity drift")
        for role, handle in self.members.items():
            member = self.config["members"][role]
            current = proc_stat(handle.read("stat"), member["pid"])
            if current["start_ticks"] != member["start_ticks"]:
                raise ValueError("process start identity drift")
            if handle.read("cgroup").strip() != "0::" + member["cgroup"]:
                raise ValueError("process cgroup identity drift")

    def _monotonic_counters(self, group: str, values: dict[str, int]) -> None:
        for key, value in values.items():
            label = group + "." + key
            if value < self._last.get(label, 0):
                raise ValueError("cumulative counter regression: " + label)
            self._last[label] = value

    def sample(self) -> dict[str, Any]:
        self.verify()
        result = {}
        for name, collect in (("api_process", self._sample_api), ("host_cgroup", self._sample_host)):
            try:
                result[name] = {"status": "supported", "reason": None, "values": collect()}
            except (KeyError, FileNotFoundError, PermissionError):
                # Missing kernel fields/files/permissions are explicit backend
                # unsupported evidence. Parsing, identity and programming
                # errors are not silently converted to availability failures.
                result[name] = {"status": "unsupported", "reason": "collector_unsupported", "values": None}
        self.verify()
        return result

    def _sample_api(self) -> dict[str, Any]:
        api = self.members["api"]
        stat = proc_stat(api.read("stat"), self.config["members"]["api"]["pid"])
        status = api.read("status")
        memory = kib_fields(status, ("VmRSS", "VmHWM"))
        threads = [line.split() for line in status.splitlines() if line.startswith("Threads:")]
        if not threads:
            raise KeyError("required thread count missing")
        if len(threads) != 1 or len(threads[0]) != 2:
            raise ValueError("malformed/duplicate thread count")
        api_values: dict[str, Any] = {
            "process_epoch_sha256": digest({"boot_id": self.config["boot_id"], **self.config["members"]["api"]}),
            "cpu_user_ns_total": stat["user_ticks"] * 1_000_000_000 // self.tick_hz,
            "cpu_system_ns_total": stat["system_ticks"] * 1_000_000_000 // self.tick_hz,
            "rss_bytes": memory["VmRSS"], "rss_hwm_bytes": memory["VmHWM"],
            "thread_count": integer(int(threads[0][1]), minimum=1),
        }
        self._monotonic_counters("api", {k: v for k, v in api_values.items() if k.endswith("_total")})
        return api_values

    def _sample_host(self) -> dict[str, Any]:
        vm = kib_fields(self.proc.read("meminfo"), ("MemTotal", "MemAvailable"))
        memory_stat = counters(self.parent.read("memory.stat"))
        events = counters(self.parent.read("memory.events"))
        cpu = counters(self.parent.read("cpu.stat"))
        self._monotonic_counters("events", events)
        self._monotonic_counters("cpu", cpu)
        maximum = self.parent.read("memory.max").strip()
        host_values = {
            "parent_cgroup_epoch_sha256": digest(self.identity),
            "docker_vm_memory_total_bytes": vm["MemTotal"], "docker_vm_memory_available_bytes": vm["MemAvailable"],
            "memory_current_bytes": integer(int(self.parent.read("memory.current"))),
            "memory_max_status": "unbounded" if maximum == "max" else "bounded",
            "memory_max_bytes": None if maximum == "max" else integer(int(maximum), minimum=1),
            "memory_stat": {key + "_bytes": memory_stat[key] for key in ("anon", "file", "shmem", "slab")},
            "memory_events": {key + "_total": events[key] for key in ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")},
            "cpu_stat": {
                "usage_ns_total": cpu["usage_usec"] * 1000, "user_ns_total": cpu["user_usec"] * 1000,
                "system_ns_total": cpu["system_usec"] * 1000, "throttled_ns_total": cpu["throttled_usec"] * 1000,
                "throttled_periods_total": cpu["nr_throttled"],
            },
        }
        for kind in ("cpu", "memory", "io"):
            psi = pressure(self.parent.read(kind + ".pressure"))
            self._monotonic_counters(kind + "_psi", {key: row["total_stall_us"] for key in ("some", "full") if (row := psi[key]) is not None})
            host_values[kind + "_psi"] = psi
        return host_values

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()


def self_cpu() -> dict[str, int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {"user_ns_total": int(usage.ru_utime * 1_000_000_000), "system_ns_total": int(usage.ru_stime * 1_000_000_000)}


def read_kernel_file(path: Path) -> str:
    parent = PinnedDirectory(path.parent)
    try:
        return parent.read(path.name)
    finally:
        parent.close()


def self_process_stat() -> dict[str, int]:
    return proc_stat(read_kernel_file(Path("/proc") / str(os.getpid()) / "stat"), os.getpid())


def host_namespaces() -> dict[str, str]:
    # A cap-drop collector cannot dereference /proc/1/ns on all Docker hosts.
    # Record its own real namespace IDs. The launch owner must independently
    # verify actual Docker PidMode/CgroupnsMode=host by exact container ID.
    result = {}
    for name in ("pid", "cgroup"):
        current = os.readlink("/proc/self/ns/" + name)
        result[name] = current
    mountinfo = read_kernel_file(Path("/proc") / str(os.getpid()) / "mountinfo")
    if not any(line.split()[4] == "/sys/fs/cgroup" and " - cgroup2 " in line for line in mountinfo.splitlines()):
        raise ValueError("actual cgroup2 mount required")
    return result


def emit(value: object, *, deadline: float, fd: int = 1) -> None:
    body = canonical(value) + b"\n"
    if len(body) > MAX_FILE_BYTES:
        raise ValueError("output frame exceeded bound")
    offset = 0
    while offset < len(body):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError("output deadline")
        try:
            offset += os.write(fd, body[offset:])
        except BlockingIOError:
            continue


def run(config: dict[str, Any], source_sha256: str) -> None:
    lease = integer(config["lease_ms"], minimum=1000) / 1000
    lifetime = integer(config["lifetime_ms"], minimum=1000) / 1000
    if lease > 30 or lifetime > 7200 or lease > lifetime:
        raise ValueError("lease/lifetime exceeds bound")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    started = time.monotonic()
    hard_end = started + lifetime
    lease_end = min(hard_end, started + lease)
    signal.setitimer(signal.ITIMER_REAL, lease_end - started)
    os.set_blocking(0, False)
    os.set_blocking(1, False)
    sampler = HostSampler(config)
    try:
        own_stat = self_process_stat()
        identity = {**sampler.identity, "source_sha256": source_sha256, "pid": os.getpid(), "start_ticks": own_stat["start_ticks"], "namespaces": host_namespaces()}
        epoch = digest(identity)
        emit({"contract_version": VERSION, "kind": "ready", "identity": identity, "epoch_sha256": epoch, "cpu": self_cpu(), "monotonic_ns": time.monotonic_ns()}, deadline=lease_end)
        buffer = bytearray()
        sequence = 0
        while True:
            remaining = min(hard_end, lease_end) - time.monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                raise TimeoutError("lease expired")
            data = os.read(0, MAX_COMMAND_BYTES + 1)
            if not data:
                return  # EOF releases the Linux owner independently of Windows.
            buffer.extend(data)
            if len(buffer) > MAX_COMMAND_BYTES:
                raise ValueError("command exceeded bound")
            if b"\n" not in buffer:
                continue
            payload, _, tail = buffer.partition(b"\n")
            if tail:
                raise ValueError("pipelined commands are forbidden")
            buffer.clear()
            command = decode(bytes(payload))
            if set(command) != {"command", "sequence"} or command["command"] not in {"sample", "close"} or integer(command["sequence"], minimum=1) != sequence + 1:
                raise ValueError("invalid command/sequence")
            sequence += 1
            lease_end = min(hard_end, time.monotonic() + lease)
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, lease_end - time.monotonic()))
            sample_start = time.monotonic_ns()
            values = sampler.sample() if command["command"] == "sample" else None
            emit({"contract_version": VERSION, "kind": command["command"], "epoch_sha256": epoch, "sequence": sequence, "started_monotonic_ns": sample_start, "finished_monotonic_ns": time.monotonic_ns(), "cpu": self_cpu(), "values": values}, deadline=lease_end)
            if command["command"] == "close":
                return
    finally:
        sampler.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-base64", required=True)
    args = parser.parse_args()
    if len(args.config_base64) > 12000:
        raise ValueError("config exceeded bound")
    config = decode(base64.b64decode(args.config_base64, validate=True))
    source = globals().get("_resident_source_bytes")
    if source is None:
        source = Path(__file__).read_bytes()
    run(config, "sha256:" + hashlib.sha256(source).hexdigest())


if __name__ == "__main__":
    main()
