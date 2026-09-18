"""Read-only replay of one resident owner's original evidence directory.

A campaign summary re-verifies the owner's own bytes instead of inheriting the
owner's verdict: the existing READY, external-observation and closure checks run
again here, over files opened without following a symlink and bounded in size.
Nothing in this module reaches the network, starts a process, or writes.

The owner's ``evidence_sha256`` index locates and pins bytes; it never grants a
pass. A file this replay cannot find, cannot match against that index, or cannot
check becomes a named problem, so the caller reports an unknown rather than a
guess. Observer mapping and combined CPU stay with the caller, which holds the
replayed observer result the objects returned here have to be bound to.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import cast

from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import sampling_duration_ns
from disclosure_anchor.application.contracts.resident_session_evidence import (
    CheckedExternalWindowsObservation, CheckedResidentClosure, CheckedResidentReady, artifact_sha256,
    check_external_windows_observation, check_resident_closure, check_resident_ready,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads

# The R22 owner writes v2 (receipt_version 4, plan and intent bound); v1 is the
# historical v3-receipt owner and stays readable. Which one was read is reported.
OWNER_RESULT_CONTRACT_VERSION = "mineru.resident-owner-diagnostic.v2"
OWNER_RESULT_CONTRACT_VERSIONS: tuple[str, ...] = (
    "mineru.resident-owner-diagnostic.v1", OWNER_RESULT_CONTRACT_VERSION,
)
# One owner evidence file is a small canonical artifact or one control stdout;
# the owner writes under the same bound, so anything larger is not its file.
MAXIMUM_OWNER_FILE_BYTES = 1024 * 1024
_LANES = ("gpu_fast", "host_slow")
_REQUIRED_FILES = ("owner-intent.json", "owner-result.json")
_SHARED_FILES = (
    "process-profile.json", "capacity-config.json", "mac-observer-identity.json",
    "sampling-plan.v1.json", "observer-plan-recorded.json", "observer-sampling-drained.json",
)
_LANE_SUFFIXES = ("-config.json", "-manifest.json", "-ready.stdout", "-start.stdout", "-closed.stdout")
# The exhaustive set this replay may open. Everything else in the directory is
# named but never read: the owner also retains stderr, exit codes, launch
# intents and its own composition sources, and none of them are evidence here.
OWNER_EVIDENCE_FILES: tuple[str, ...] = (
    *_REQUIRED_FILES, *_SHARED_FILES,
    *(lane + suffix for lane in _LANES for suffix in _LANE_SUFFIXES),
)


@dataclass(frozen=True, slots=True)
class ResidentOwnerEvidence:
    """What the owner's original files still prove when replayed on their own.

    ``files`` records the SHA-256 this replay actually computed over the bytes it
    read, not the owner's claim about them. ``problems`` names every absence,
    index mismatch and failed check; an empty tuple is the only silence.
    """

    directory: Path
    run_id: str
    owner_intent_sha256: str
    result_contract_version: str
    receipt_version: int | None
    # The original intent's declared duration, in exact nanoseconds, so a summary
    # can pin the frozen plan to what the owner actually asked for.
    intent_duration_ns: int | None
    files: dict[str, str]
    readies: dict[str, CheckedResidentReady]
    closures: dict[str, CheckedResidentClosure]
    ready_observations: dict[str, CheckedExternalWindowsObservation]
    closed_payloads: dict[str, bytes]
    plan_bytes: bytes | None
    problems: tuple[str, ...]


def _read_bounded(directory_fd: int, name: str) -> bytes:
    """Read one whole named file anchored to the original directory descriptor.

    ``O_NOFOLLOW`` and the regular-file check keep a replaced entry from
    redirecting this read; the bound keeps an unexpected file from being loaded.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        # A symlink, a device or an unreadable entry under an evidence name is
        # not a missing measurement: the owner creates these files exclusively
        # and never as a link, so a redirected name stays a visible failure.
        raise ValueError(f"owner evidence {name} is not a readable regular file: {error.strerror}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"owner evidence {name} is not a regular file")
        if info.st_size > MAXIMUM_OWNER_FILE_BYTES:
            raise ValueError(f"owner evidence {name} exceeds the {MAXIMUM_OWNER_FILE_BYTES}-byte bound")
        chunks: list[bytes] = []
        total = 0
        while total <= MAXIMUM_OWNER_FILE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAXIMUM_OWNER_FILE_BYTES:
            raise ValueError(f"owner evidence {name} exceeds the {MAXIMUM_OWNER_FILE_BYTES}-byte bound")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"owner evidence {label} is not a JSON object")
    return cast(dict[str, object], value)


def _text(value: dict[str, object], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"owner evidence {label} lacks a {key} string")
    return item


def _raw_artifact(payload: bytes, key: str, label: str) -> bytes:
    """Take one embedded original artifact out of a pinned control's stdout."""
    return _text(_mapping(strict_json_loads(payload), label), key, label).encode()


def _source_hashes(intent: dict[str, object]) -> Mapping[str, str]:
    hashes = _mapping(intent.get("source_hashes"), "owner-intent.json source_hashes")
    if any(not isinstance(value, str) for value in hashes.values()):
        raise ValueError("owner evidence intent source hashes are not strings")
    return cast(Mapping[str, str], hashes)


def _evidence_index(result: dict[str, object]) -> Mapping[str, str]:
    index = _mapping(result.get("evidence_sha256"), "owner-result.json evidence_sha256")
    if any(not isinstance(value, str) for value in index.values()):
        raise ValueError("owner evidence index entries are not strings")
    return cast(Mapping[str, str], index)


@dataclass(frozen=True, slots=True)
class _LaneReplay:
    """Collected per-lane results and problems, before the frozen evidence exists."""

    readies: dict[str, CheckedResidentReady]
    closures: dict[str, CheckedResidentClosure]
    ready_observations: dict[str, CheckedExternalWindowsObservation]
    closed_payloads: dict[str, bytes]
    problems: list[str]


def _replay_lane(
    lane: str, *, payloads: Mapping[str, bytes], source_hashes: Mapping[str, str],
    windows_node_identity_sha256: str | None, collected: _LaneReplay,
) -> None:
    """Re-run the owner's own checks for one lane; absence and failure are named."""
    config = payloads.get(lane + "-config.json")
    manifest = payloads.get(lane + "-manifest.json")
    ready_stdout = payloads.get(lane + "-ready.stdout")
    problems = collected.problems
    if config is None or manifest is None or ready_stdout is None:
        return
    try:
        ready = check_resident_ready(
            config_bytes=config, ready_bytes=_raw_artifact(ready_stdout, "ready_raw", lane + "-ready.stdout"),
            manifest_bytes=manifest, expected_source_hashes=source_hashes,
        )
    except ValueError as error:
        problems.append(f"owner_check_failed:{lane}:check_resident_ready:{error}")
        return
    collected.readies[lane] = ready
    if windows_node_identity_sha256 is not None:
        try:
            collected.ready_observations[lane] = check_external_windows_observation(
                payload=ready_stdout, ready=ready,
                windows_node_identity_sha256=windows_node_identity_sha256,
            )
        except ValueError as error:
            problems.append(f"owner_check_failed:{lane}:check_external_windows_observation:{error}")
    closed_stdout = payloads.get(lane + "-closed.stdout")
    if closed_stdout is None:
        return
    label = lane + "-closed.stdout"
    try:
        closed_raw = _raw_artifact(closed_stdout, "closed_raw", label)
        job_raw = _raw_artifact(closed_stdout, "job_raw", label)
        linux_raw = _raw_artifact(closed_stdout, "linux_closed_raw", label) if lane == "host_slow" else None
    except ValueError as error:
        problems.append(f"owner_check_failed:{lane}:check_resident_closure:{error}")
        return
    collected.closed_payloads[lane] = closed_raw
    # The same cross-check the online owner performs before accepting a closure:
    # the starter's retained stdout is the original Job document the independent
    # closed observation reread. A different stdout is a different session.
    start_stdout = payloads.get(lane + "-start.stdout")
    if start_stdout is not None and start_stdout.rstrip(b"\r\n") != job_raw:
        problems.append(f"owner_check_failed:{lane}:starter_stdout_differs_from_job")
    previous = collected.ready_observations.get(lane)
    if windows_node_identity_sha256 is not None and previous is not None:
        try:
            check_external_windows_observation(
                payload=closed_stdout, ready=ready,
                windows_node_identity_sha256=windows_node_identity_sha256, previous_ready=previous,
            )
        except ValueError as error:
            problems.append(f"owner_check_failed:{lane}:check_external_windows_observation:{error}")
    try:
        collected.closures[lane] = check_resident_closure(
            ready=ready, closed_bytes=closed_raw, job_bytes=job_raw, linux_closed_bytes=linux_raw,
        )
    except ValueError as error:
        problems.append(f"owner_check_failed:{lane}:check_resident_closure:{error}")


class ResidentOwnerEvidenceForeign(ValueError):
    """The directory is another run's owner evidence: the wrong input, not a gap in this run's."""


def replay_resident_owner_evidence(
    directory: Path, *, run_id: str, windows_node_identity_sha256: str | None,
) -> ResidentOwnerEvidence:
    """Re-verify one owner's original evidence directory without any live access.

    ``owner-intent.json`` and ``owner-result.json`` must be there and must name
    ``run_id``: a directory that belongs to another run is the wrong directory,
    not a partial one, and is refused rather than reported. Every other file is
    optional here - a v3 owner has no sampling plan, and a packet can be
    incomplete - so its absence, its index mismatch and any check it fails are
    named in ``problems`` and the corresponding object is simply missing.

    ``check_resident_supervisor_started`` is not replayable from these files:
    the owner retains the pre-Job marker only inside the control observation it
    already checks, so nothing here re-derives it.
    """
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise ValueError("resident owner evidence directory must be an absolute path")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("resident owner evidence replay requires the expected run identifier")
    if windows_node_identity_sha256 is not None and re.fullmatch(
        r"sha256:[0-9a-f]{64}", windows_node_identity_sha256
    ) is None:
        raise ValueError("resident owner evidence Windows node identity is malformed")

    problems: list[str] = []
    payloads: dict[str, bytes] = {}
    files: dict[str, str] = {}
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"resident owner evidence directory is not readable: {error.strerror}") from error
    try:
        for name in OWNER_EVIDENCE_FILES:
            try:
                payload = _read_bounded(directory_fd, name)
            except FileNotFoundError:
                if name in _REQUIRED_FILES:
                    raise ValueError(f"resident owner evidence lacks {name}") from None
                problems.append(f"owner_evidence_absent:{name}")
                continue
            payloads[name] = payload
            files[name] = artifact_sha256(payload)
        present = set(os.listdir(directory_fd))
    finally:
        os.close(directory_fd)

    intent = _mapping(strict_json_loads(payloads["owner-intent.json"]), "owner-intent.json")
    result = _mapping(strict_json_loads(payloads["owner-result.json"]), "owner-result.json")
    result_version = result.get("contract_version")
    if result_version not in OWNER_RESULT_CONTRACT_VERSIONS:
        raise ValueError("resident owner evidence result contract version differs")
    index = _evidence_index(result)
    if _text(intent, "run_id", "owner-intent.json") != run_id or _text(result, "run_id", "owner-result.json") != run_id:
        raise ResidentOwnerEvidenceForeign("resident owner evidence belongs to another run")
    for name, digest in files.items():
        # The owner writes its result last, so its index cannot contain itself.
        if name != "owner-result.json" and index.get(name) != digest:
            problems.append(f"owner_evidence_hash_mismatch:{name}")
    receipt_version = result.get("receipt_version")
    if type(receipt_version) is not int:
        receipt_version = None
        problems.append("owner_result_receipt_version_absent")
    if result_version == OWNER_RESULT_CONTRACT_VERSION:
        # v2 binds the owner's own claims to the files replayed here: the exact
        # plan bytes, the exact intent bytes and the v4 receipt protocol.
        if receipt_version != 4:
            problems.append("owner_result_receipt_version_differs")
        if result.get("owner_intent_sha256") != files["owner-intent.json"]:
            problems.append("owner_result_intent_mismatch")
        plan_payload = payloads.get("sampling-plan.v1.json")
        if plan_payload is not None and result.get("sampling_plan_sha256") != artifact_sha256(plan_payload):
            problems.append("owner_result_plan_mismatch")
    # Unknown files are listed, never opened: the owner's directory also holds
    # control stderr and launch intents, and this replay does not read them.
    accounted = set(OWNER_EVIDENCE_FILES) | set(index)
    problems.extend(f"unexpected_owner_file:{name}" for name in present - accounted)

    intent_duration_ns: int | None = None
    try:
        intent_duration_ns = sampling_duration_ns(cast(float, intent.get("duration_seconds")))
    except ValueError:
        problems.append("owner_intent_duration_invalid")
    if windows_node_identity_sha256 is None:
        problems.append("windows_node_identity_unavailable")
    collected = _LaneReplay({}, {}, {}, {}, problems)
    source_hashes = _source_hashes(intent)
    for lane in _LANES:
        _replay_lane(
            lane, payloads=payloads, source_hashes=source_hashes,
            windows_node_identity_sha256=windows_node_identity_sha256, collected=collected,
        )
    return ResidentOwnerEvidence(
        directory=directory, run_id=run_id, owner_intent_sha256=files["owner-intent.json"],
        result_contract_version=cast(str, result_version), receipt_version=receipt_version,
        intent_duration_ns=intent_duration_ns, files=files,
        readies=collected.readies, closures=collected.closures,
        ready_observations=collected.ready_observations, closed_payloads=collected.closed_payloads,
        plan_bytes=payloads.get("sampling-plan.v1.json"), problems=tuple(sorted(set(problems))),
    )


__all__ = [
    "MAXIMUM_OWNER_FILE_BYTES",
    "OWNER_EVIDENCE_FILES",
    "OWNER_RESULT_CONTRACT_VERSION",
    "OWNER_RESULT_CONTRACT_VERSIONS",
    "ResidentOwnerEvidence",
    "ResidentOwnerEvidenceForeign",
    "replay_resident_owner_evidence",
]
