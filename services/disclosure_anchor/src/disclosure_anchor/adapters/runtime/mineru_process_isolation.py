"""Read-only process and temporary-directory isolation observations for MinerU gates."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile


_PRODUCER_MARKERS = (
    "-m disclosure_anchor.cli.worker",
    "-m disclosure_anchor.cli.pipeline",
    "-m uvicorn disclosure_anchor.main:",
    "uvicorn disclosure_anchor.main:",
)


def process_snapshot() -> dict[int, str]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cannot inspect processes for MinerU isolation")
    processes: dict[int, str] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        processes[int(fields[0])] = fields[1]
    return processes


def active_disclosure_producers(processes: dict[int, str]) -> dict[int, str]:
    return {
        pid: command
        for pid, command in processes.items()
        if any(marker in command for marker in _PRODUCER_MARKERS)
    }


def mineru_processes(processes: dict[int, str]) -> dict[int, str]:
    return {
        pid: command
        for pid, command in processes.items()
        if _is_mineru_process(command)
    }


def mineru_api_temp_dirs() -> set[Path]:
    roots = {
        Path(tempfile.gettempdir()).resolve(strict=False),
        Path("/tmp").resolve(strict=False),
        Path("/private/tmp").resolve(strict=False),
    }
    return {
        path.resolve(strict=False)
        for root in roots
        if root.is_dir()
        for path in root.glob("mineru-api-client-*")
    }


def _is_mineru_process(command: str) -> bool:
    argv = command.split()
    if not argv:
        return False
    if any(
        Path(value).name.lower() in {"mineru", "mineru-api"}
        for value in argv[:3]
    ):
        return True
    if "mineru-api-client-" in command:
        return True
    if any(value.lower().startswith("mineru.") for value in argv):
        return True
    return any(
        value == "-m"
        and index + 1 < len(argv)
        and argv[index + 1].lower().startswith("mineru.")
        for index, value in enumerate(argv)
    )


__all__ = [
    "active_disclosure_producers",
    "mineru_api_temp_dirs",
    "mineru_processes",
    "process_snapshot",
]
