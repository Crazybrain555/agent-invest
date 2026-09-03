"""Restart-safe HTTP/filesystem backend for the pure remote-parse v4 port.

The module deliberately contains no provider URL, database, settings, or worker
lookups.  Its caller supplies already-authorized evidence, a private transport,
the scratch root, and the under-lock claim guard.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import threading
from typing import Any, Iterator, Protocol, cast
import unicodedata
import zipfile
import zlib

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
    PinnedArtifactTree,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    decode_local_materialization_manifest_v4,
    seal_local_materialization_manifest_v4,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_from_bytes,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    PreparationIntentV4,
    TerminalReceiptV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalCleanupPlanV4,
    LocalCleanupReceiptV4,
    LocalCleanupResourceResultV4,
    LocalMaterializationReceiptV4,
    LocalOutputFileV4,
    MaterializationIntentV4,
    ProviderAckReceiptV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    build_local_cleanup_receipt_v4,
    build_local_materialization_receipt_v4,
    local_output_files_sha256_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
)
from disclosure_anchor.application.contracts.staged_resource_paths import (
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    PrivateProviderCapabilityV4,
    ProviderAckCommandV4,
    V4ClaimGuard,
    V4ClaimWitness,
    V4EvidenceReplayContext,
    validate_v4_ack_authorization,
    validate_v4_cleanup_authorization,
    validate_v4_materialization_authorization,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_CHUNK_BYTES = 1024 * 1024
_LOCK_SCHEMA = "mineru-v4-resource-lock.v1"
_OWNER_SCHEMA = "mineru-v4-spool-owner.v1"
_MARKER_SCHEMA = "mineru-v4-materialization-marker.v1"
_MAX_METADATA_BYTES = 64 * 1024
_MAX_ACK_RESPONSE_BYTES = 64 * 1024
_MAX_RECOVERY_PATH_PARTS = 32


@dataclass(frozen=True, slots=True)
class _StableFileStat:
    device: int
    inode: int
    mode: int
    uid: int
    link_count: int
    byte_count: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> _StableFileStat:
        return cls(
            device=observed.st_dev,
            inode=observed.st_ino,
            mode=observed.st_mode,
            uid=observed.st_uid,
            link_count=observed.st_nlink,
            byte_count=observed.st_size,
            modified_ns=observed.st_mtime_ns,
            changed_ns=observed.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _ActiveLockRecord:
    parent_path: Path
    parent_fd: int
    name: str
    observed: os.stat_result
    kind: str


@dataclass(frozen=True, slots=True)
class _JournalFile:
    identity: _StableFileStat
    sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedZipMember:
    archive_name: str
    archive_original_name: str
    normalized_name: str
    is_directory: bool
    file_size: int
    compressed_size: int
    crc32: int
    external_attr: int
    header_offset: int
    flag_bits: int
    compression: int

    def __post_init__(self) -> None:
        if (
            type(self.archive_name) is not str
            or type(self.archive_original_name) is not str
            or type(self.normalized_name) is not str
            or "\x00" in self.archive_name
            or "\x00" in self.archive_original_name
            or "\x00" in self.normalized_name
            or self.archive_original_name != self.archive_name
        ):
            raise ValueError("ZIP member name evidence is not exact")

    @classmethod
    def from_info(
        cls,
        info: zipfile.ZipInfo,
        *,
        normalized_name: str,
    ) -> _ValidatedZipMember:
        return cls(
            archive_name=info.filename,
            archive_original_name=info.orig_filename,
            normalized_name=normalized_name,
            is_directory=info.is_dir(),
            file_size=info.file_size,
            compressed_size=info.compress_size,
            crc32=info.CRC,
            external_attr=info.external_attr,
            header_offset=info.header_offset,
            flag_bits=info.flag_bits,
            compression=info.compress_type,
        )

    def validates(self, info: zipfile.ZipInfo) -> bool:
        if (
            type(info.filename) is not str
            or type(info.orig_filename) is not str
            or "\x00" in info.filename
            or "\x00" in info.orig_filename
            or info.orig_filename != info.filename
        ):
            return False
        try:
            observed = _ValidatedZipMember.from_info(
                info,
                normalized_name=self.normalized_name,
            )
        except ValueError:
            return False
        return self == observed


@dataclass(frozen=True, slots=True)
class _ValidatedZipMetadata:
    members: tuple[_ValidatedZipMember, ...]
    uncompressed_byte_count: int


@dataclass(slots=True)
class _ZipPathCollisionIndex:
    """O(total path parts) casefold collision and file-ancestor index."""

    member_keys: set[tuple[str, ...]]
    file_keys: set[tuple[str, ...]]
    strict_prefixes: set[tuple[str, ...]]
    probe_count: int = 0

    @classmethod
    def empty(cls) -> _ZipPathCollisionIndex:
        return cls(member_keys=set(), file_keys=set(), strict_prefixes=set())

    def admit(self, key: tuple[str, ...], *, is_directory: bool) -> bool:
        self.probe_count += 1
        if key in self.member_keys:
            return False
        for depth in range(1, len(key)):
            self.probe_count += 1
            if key[:depth] in self.file_keys:
                return False
        if not is_directory:
            self.probe_count += 1
            if key in self.strict_prefixes:
                return False
        self.member_keys.add(key)
        if not is_directory:
            self.file_keys.add(key)
        for depth in range(1, len(key)):
            self.probe_count += 1
            self.strict_prefixes.add(key[:depth])
        return True


@dataclass(slots=True)
class _StagingWriteJournal:
    files: dict[PurePosixPath, _JournalFile]
    directories: set[PurePosixPath]
    mutation_started: bool = False

    @classmethod
    def empty(cls) -> _StagingWriteJournal:
        return cls(files={}, directories=set())

    def add_directory(self, relative: PurePosixPath) -> None:
        if relative in self.directories:
            return
        self.directories.add(relative)

    def add_file(
        self,
        relative: PurePosixPath,
        *,
        observed: os.stat_result,
        sha256: str,
    ) -> None:
        if relative in self.files:
            raise ParserOutputContractError(
                "MinerU v4 backend: staging journal file is duplicated"
            )
        self.files[relative] = _JournalFile(
            identity=_StableFileStat.from_stat(observed),
            sha256=sha256,
        )


class _RootLockCoordinator:
    def __init__(self) -> None:
        self.process_lock = threading.RLock()
        self.local = threading.local()


_ROOT_COORDINATORS_GUARD = threading.Lock()
_ROOT_COORDINATORS: dict[tuple[int, int], _RootLockCoordinator] = {}


def _root_coordinator(identity: tuple[int, int]) -> _RootLockCoordinator:
    with _ROOT_COORDINATORS_GUARD:
        return _ROOT_COORDINATORS.setdefault(identity, _RootLockCoordinator())


class MinerUV4Transport(Protocol):
    """Private provider operations; implementations must not inherit proxy env."""

    def stream_result(
        self,
        *,
        accepted_submission: AcceptedSubmissionReceiptV4,
        provider_capability: PrivateProviderCapabilityV4,
    ) -> Iterable[bytes]: ...

    def acknowledge(
        self,
        *,
        command: ProviderAckCommandV4,
        provider_capability: PrivateProviderCapabilityV4,
    ) -> "MinerUV4HttpResponse": ...


@dataclass(frozen=True, slots=True)
class MinerUV4HttpResponse:
    status_code: int
    exact_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or type(self.exact_bytes) is not bytes:
            raise ValueError("v4 HTTP response envelope drifted")


class MinerUHttpStagedV4:
    """One restart-safe implementation of all three v4 side-effect domains."""

    def __init__(
        self,
        *,
        scratch_root: Path,
        transport: MinerUV4Transport,
        clock: Callable[[], float],
        artifact_reader: MinerUMediumArtifactReader | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(scratch_root, Path) or not scratch_root.is_absolute():
            raise ValueError("v4 scratch root must be an absolute Path")
        self._root = scratch_root
        self._transport = transport
        self._clock = clock
        self._reader = artifact_reader or MinerUMediumArtifactReader()
        self._fault_hook = fault_hook or (lambda _phase: None)
        self._ensure_root()
        observed_root = self._root.stat(follow_symlinks=False)
        self._root_identity = (
            observed_root.st_dev,
            observed_root.st_ino,
            observed_root.st_uid,
            observed_root.st_mode,
        )
        self._root_lock_coordinator = _root_coordinator(
            (observed_root.st_dev, observed_root.st_ino)
        )
        self._active_locks = threading.local()
        self._name_max = int(os.pathconf(self._root, "PC_NAME_MAX"))

    def materialize_v4(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        preparation_intent: PreparationIntentV4,
        intent: MaterializationIntentV4,
        accepted_submission: AcceptedSubmissionReceiptV4,
        terminal_receipt: TerminalReceiptV4,
        provider_capability: PrivateProviderCapabilityV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        allowance: PerAttemptResourceAllowance,
        replay_context: V4EvidenceReplayContext,
    ) -> MaterializedProviderDocumentV4:
        self._observe_clock()
        validate_v4_materialization_authorization(
            checkpoint=checkpoint,
            reservation=reservation,
            preparation_intent=preparation_intent,
            intent=intent,
            accepted_submission=accepted_submission,
            terminal_receipt=terminal_receipt,
            provider_capability=provider_capability,
            claim=claim,
            allowance=allowance,
            replay_context=replay_context,
        )
        if intent.output_manifest_relpath != LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME:
            raise self._fail("v4 output manifest must be rooted at its fixed basename")
        spool = self._path(intent.spool_relpath)
        spool_part = self._path(intent.spool_part_relpath)
        spool_owner = self._path(intent.spool_part_owner_relpath)
        spool_lock = self._path(intent.spool_lock_relpath)
        staging = self._path(intent.staging_relpath)
        marker = self._path(intent.staging_marker_relpath)
        staging_lock = self._path(intent.staging_lock_relpath)
        output = self._path(intent.output_relpath)
        lock_binding = self._resource_binding(intent)

        # Lock order is a contract: the spool lock is never acquired after the
        # staging lock.  Both remain held through publication/replay.
        with self._locked(spool_lock, "spool", lock_binding):
            self._guard(claim_guard, checkpoint, claim)
            spool_identity = self._ensure_spool(
                checkpoint=checkpoint,
                claim=claim,
                claim_guard=claim_guard,
                intent=intent,
                accepted=accepted_submission,
                capability=provider_capability,
                spool=spool,
                part=spool_part,
                owner=spool_owner,
            )
            # The same exact, hashed spool fd remains pinned from metadata
            # preflight through extraction.  Entering this context performs
            # all metadata-only checks before the staging lock or namespace is
            # created; extraction still repeats those checks on the pinned
            # archive and verifies the fd/path identity around its reads.
            with (
                self._preflighted_provider_zip(
                    spool=spool,
                    intent=intent,
                    expected_identity=spool_identity,
                ) as zip_session,
                self._locked(staging_lock, "staging", lock_binding),
            ):
                archive, zip_metadata, verify_spool = zip_session
                self._guard(claim_guard, checkpoint, claim)
                if output.exists() or output.is_symlink():
                    replayed = self._replay_or_recover_promoted_output(
                        checkpoint=checkpoint,
                        claim=claim,
                        claim_guard=claim_guard,
                        intent=intent,
                        output=output,
                        staging=staging,
                    )
                    if replayed is not None:
                        return replayed
                self._classify_and_resolve_existing_staging(
                    checkpoint=checkpoint,
                    claim=claim,
                    claim_guard=claim_guard,
                    intent=intent,
                    staging=staging,
                    staging_lock=staging_lock,
                )
                journal = _StagingWriteJournal.empty()
                self._prepare_staging(
                    intent=intent,
                    staging=staging,
                    marker=marker,
                    journal=journal,
                )
                try:
                    try:
                        observations = self._materialize_staging(
                            intent=intent,
                            archive=archive,
                            zip_metadata=zip_metadata,
                            verify_spool=verify_spool,
                            staging=staging,
                            marker=marker,
                            journal=journal,
                            before_destructive=lambda: self._guard(
                                claim_guard, checkpoint, claim
                            ),
                        )
                    except (ParserOutputContractError, ValueError) as exc:
                        self._resolve_failed_staging_write(
                            checkpoint=checkpoint,
                            claim=claim,
                            claim_guard=claim_guard,
                            intent=intent,
                            staging=staging,
                            staging_lock=staging_lock,
                            journal=journal,
                        )
                        if isinstance(exc, ParserOutputContractError):
                            raise
                        raise self._fail(
                            "provider result semantics are invalid"
                        ) from exc
                    verify_spool()
                    with self._pinned_tree(
                        staging,
                        max_files=intent.member_count_limit + 3,
                        max_bytes=intent.output_byte_limit + _MAX_METADATA_BYTES,
                    ) as sealed_tree:
                        projected = self._materialized_from_tree(
                            intent=intent,
                            tree=sealed_tree,
                            allow_marker=True,
                            staging_absent=True,
                        )
                        if (
                            projected.receipt.member_count != observations.member_count
                            or projected.receipt.uncompressed_byte_count
                            != observations.uncompressed_byte_count
                        ):
                            raise self._fail("sealed observations drifted")
                        sealed_identity = sealed_tree.root_identity
                        sealed_tree.fsync_exact()
                        self._fault_hook("after_staging_fsync")
                        sealed_tree.verify_unchanged()
                        self._guard(claim_guard, checkpoint, claim)
                        if output.exists() or output.is_symlink():
                            raise self._fail("materialization output collision")

                        def before_promotion() -> None:
                            self._guard(claim_guard, checkpoint, claim)
                            sealed_tree.verify_unchanged()

                        self._exclusive_rename(
                            staging,
                            output,
                            expected_source_identity=sealed_identity,
                            before_rename=before_promotion,
                            after_rename=lambda: self._fault_hook(
                                "after_promotion_rename"
                            ),
                        )
                    self._fault_hook("after_promotion")
                    self._load_exact_output(
                        intent=intent,
                        output=output,
                        allow_marker=True,
                        expected_output_identity=sealed_identity,
                        fsync_exact=True,
                    )
                    self._finish_promoted_marker(
                        checkpoint=checkpoint,
                        claim=claim,
                        claim_guard=claim_guard,
                        intent=intent,
                        output=output,
                        expected_output_identity=sealed_identity,
                    )
                    value = self._load_exact_output(
                        intent=intent,
                        output=output,
                        expected_output_identity=sealed_identity,
                        fsync_exact=True,
                    )
                    if (
                        value.receipt.member_count != observations.member_count
                        or value.receipt.uncompressed_byte_count
                        != observations.uncompressed_byte_count
                    ):
                        raise self._fail("promoted observations drifted")
                    return value
                except BaseException:
                    # Exact staging is intentionally retained for restart.  A
                    # later call removes it only while holding both locks and
                    # after revalidating the claim.
                    raise

    def _replay_or_recover_promoted_output(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        output: Path,
        staging: Path,
    ) -> MaterializedProviderDocumentV4 | None:
        observed = self._try_path_stat(output)
        if observed is None:
            return None
        self._require_owned_dir_stat(observed, "promoted materialization output")
        if self._try_path_stat(staging) is not None:
            raise self._fail("materialization output and staging both exist")
        output_identity = self._identity(observed)
        marker = output / PurePosixPath(intent.staging_marker_relpath).name
        marker_stat = self._try_path_stat(marker)
        if marker_stat is None:
            return self._load_exact_output(
                intent=intent,
                output=output,
                expected_output_identity=output_identity,
                fsync_exact=True,
            )
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or self._read_private(marker) != self._marker_bytes(intent)
        ):
            raise self._fail("promoted materialization marker drifted")
        try:
            self._load_exact_output(
                intent=intent,
                output=output,
                allow_marker=True,
                expected_output_identity=output_identity,
                fsync_exact=True,
            )
        except (ParserOutputContractError, ValueError):
            operation_started = False
            try:
                with self._pinned_tree(
                    output,
                    max_files=intent.member_count_limit + 3,
                    max_bytes=self._recovery_scan_max_bytes(intent),
                ) as recovery_tree:
                    if recovery_tree.root_identity != output_identity:
                        raise self._fail(
                            "promoted materialization output identity drifted"
                        )
                    self._validate_recovery_tree_admission(
                        intent=intent,
                        tree=recovery_tree,
                        require_full=True,
                    )
                    self._materialized_from_tree(
                        intent=intent,
                        tree=recovery_tree,
                        allow_marker=True,
                        staging_absent=True,
                    )
                    operation_started = True

                    def before_recovery_rename() -> None:
                        self._guard(claim_guard, checkpoint, claim)
                        self._fault_hook("before_invalid_output_recovery_rename")
                        self._guard(claim_guard, checkpoint, claim)
                        recovery_tree.verify_pinned_topology_unchanged()

                    def after_recovery_rename() -> None:
                        self._fault_hook("after_invalid_output_recovery_rename")
                        recovery_tree.verify_pinned_topology_unchanged(
                            allow_root_rename_ctime=True
                        )

                    self._exclusive_rename(
                        output,
                        staging,
                        expected_source_identity=recovery_tree.root_identity,
                        before_rename=before_recovery_rename,
                        after_rename=after_recovery_rename,
                    )
                    recovery_tree.verify_pinned_topology_unchanged(
                        allow_root_rename_ctime=True
                    )
                    self._fault_hook("after_invalid_output_recovery")
                    recovery_tree.verify_pinned_topology_unchanged(
                        allow_root_rename_ctime=True
                    )
                    self._remove_admitted_recovery_tree(
                        checkpoint=checkpoint,
                        claim=claim,
                        claim_guard=claim_guard,
                        intent=intent,
                        staging=staging,
                        tree=recovery_tree,
                    )
            except (ParserOutputContractError, ValueError):
                if operation_started:
                    raise
                self._quarantine_marker_bound_tree(
                    checkpoint=checkpoint,
                    claim=claim,
                    claim_guard=claim_guard,
                    intent=intent,
                    source=output,
                    staging_lock=self._path(intent.staging_lock_relpath),
                    expected_identity=output_identity,
                    before_phase="before_invalid_output_quarantine_rename",
                    after_phase="after_invalid_output_quarantine_rename",
                )
            return None
        self._finish_promoted_marker(
            checkpoint=checkpoint,
            claim=claim,
            claim_guard=claim_guard,
            intent=intent,
            output=output,
            expected_output_identity=output_identity,
        )
        return self._load_exact_output(
            intent=intent,
            output=output,
            expected_output_identity=output_identity,
            fsync_exact=True,
        )

    def _validate_recovery_tree_admission(
        self,
        *,
        intent: MaterializationIntentV4,
        tree: PinnedArtifactTree,
        require_full: bool = False,
    ) -> None:
        marker_file = PurePosixPath(
            PurePosixPath(intent.staging_marker_relpath).name
        )
        if tree.read_bytes(
            marker_file,
            max_bytes=_MAX_METADATA_BYTES,
        ) != self._marker_bytes(intent):
            raise self._fail("promoted materialization marker drifted")
        manifest_file = tree.require_file(
            PurePosixPath(LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME)
        )
        manifest = decode_local_materialization_manifest_v4(
            tree.read_bytes(
                manifest_file.relative_path,
                max_bytes=intent.output_byte_limit,
            )
        )
        declared_files = tuple(
            sorted(
                (
                    *(
                        LocalOutputFileV4(
                            relpath=item.relpath,
                            sha256=item.sha256,
                            byte_count=item.byte_count,
                        )
                        for item in manifest.payload_files
                    ),
                    LocalOutputFileV4(
                        relpath=manifest_file.relative_path.as_posix(),
                        sha256=manifest.sha256,
                        byte_count=len(manifest.canonical_bytes),
                    ),
                ),
                key=lambda item: item.relpath,
            )
        )
        build_local_materialization_receipt_v4(
            intent=intent,
            manifest=manifest,
            source_page_count=intent.source_page_count,
            output_files=declared_files,
            provider_envelope_relpath=intent.provider_envelope_relpath,
            output_manifest_relpath=intent.output_manifest_relpath,
            member_count=manifest.observations.member_count,
            uncompressed_byte_count=manifest.observations.uncompressed_byte_count,
            decoded_byte_count=manifest.observations.decoded_byte_count,
            temporary_disk_peak_byte_count=(
                manifest.observations.temporary_disk_peak_byte_count
            ),
            file_fsync_completed=True,
            output_parent_fsync_completed=True,
            marker_removed=True,
            spool_part_absent=True,
            spool_part_owner_absent=True,
            staging_absent=True,
        )
        marker_bytes = self._marker_bytes(intent)
        expected_files = {
            **{
                PurePosixPath(item.relpath): (item.sha256, item.byte_count)
                for item in declared_files
            },
            marker_file: (
                "sha256:" + hashlib.sha256(marker_bytes).hexdigest(),
                len(marker_bytes),
            ),
        }
        self._validate_exact_cleanup_projection(
            tree=tree,
            expected_files=expected_files,
            last_files=(
                PurePosixPath(intent.output_manifest_relpath),
                marker_file,
            ),
            require_full=require_full,
        )
        tree.verify_pinned_topology_unchanged()

    def _validate_exact_cleanup_projection(
        self,
        *,
        tree: PinnedArtifactTree,
        expected_files: dict[PurePosixPath, tuple[str, int]],
        last_files: tuple[PurePosixPath, ...],
        require_full: bool,
    ) -> None:
        observed = {
            item.relative_path: (item.sha256, item.size_bytes)
            for item in tree.files
        }
        if any(
            path not in expected_files or expected_files[path] != identity
            for path, identity in observed.items()
        ):
            raise self._fail("recovery tree contains undeclared or mutated files")
        expected_paths = set(expected_files)
        observed_paths = set(observed)
        full_directories = {PurePosixPath(".")}
        for file_path in expected_paths:
            parent = file_path.parent
            while parent != PurePosixPath("."):
                full_directories.add(parent)
                parent = parent.parent
        observed_directories = set(tree.directory_paths)
        if require_full:
            if (
                observed_paths != expected_paths
                or observed_directories != full_directories
            ):
                raise self._fail("recovery tree is not the full exact projection")
            return

        last_set = set(last_files)
        if not last_set <= expected_paths:
            raise self._fail("recovery proof files are outside the exact projection")
        ordinary_order = tuple(sorted(expected_paths - last_set))
        observed_ordinary = tuple(
            path for path in ordinary_order if path in observed_paths
        )
        if observed_ordinary != ordinary_order[
            len(ordinary_order) - len(observed_ordinary) :
        ]:
            raise self._fail("recovery files are not a deterministic cleanup suffix")
        if observed_ordinary:
            if (
                not last_set <= observed_paths
                or observed_directories != full_directories
            ):
                raise self._fail("recovery file cleanup topology is not exact")
            return

        observed_last = tuple(path for path in last_files if path in observed_paths)
        if (
            observed_last != last_files[len(last_files) - len(observed_last) :]
            or observed_paths != set(observed_last)
        ):
            raise self._fail("recovery proof files are not a cleanup suffix")
        directory_order = tuple(
            sorted(
                full_directories - {PurePosixPath(".")},
                key=lambda path: (len(path.parts), path.as_posix()),
                reverse=True,
            )
        )
        if not observed_directories <= full_directories:
            raise self._fail("recovery directories are not a cleanup suffix")
        missing_directories = full_directories - observed_directories
        if missing_directories != set(
            directory_order[: len(missing_directories)]
        ):
            raise self._fail("recovery directories are not a cleanup suffix")
        if observed_directories != {PurePosixPath(".")} and observed_last != last_files:
            raise self._fail("recovery proof deletion preceded directory cleanup")

    def _classify_and_resolve_existing_staging(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        staging: Path,
        staging_lock: Path,
    ) -> bool:
        observed = self._try_path_stat(staging)
        if observed is None:
            return False
        self._require_owned_dir_stat(observed, "materialization recovery staging")
        self._require_lock_binding(
            staging_lock,
            "staging",
            self._resource_binding(intent),
        )
        manifest_path = staging / intent.output_manifest_relpath
        marker_file = PurePosixPath(PurePosixPath(intent.staging_marker_relpath).name)
        marker_path = staging / marker_file
        manifest_stat = self._try_path_stat(manifest_path)
        marker_stat = self._try_path_stat(marker_path)
        if marker_stat is None:
            with self._pinned_tree(
                staging,
                max_files=intent.member_count_limit + 3,
                max_bytes=intent.output_byte_limit + _MAX_METADATA_BYTES,
                allow_empty_directories=True,
            ) as recovery_tree:
                if recovery_tree.root_identity != self._identity(observed):
                    raise self._fail(
                        "materialization recovery staging identity drifted"
                    )
                if recovery_tree.files or set(recovery_tree.directory_paths) != {
                    PurePosixPath(".")
                }:
                    raise self._fail("markerless staging is not exactly empty")

                def before_empty_rmdir() -> None:
                    self._guard(claim_guard, checkpoint, claim)
                    self._fault_hook("before_empty_staging_rmdir")
                    self._guard(claim_guard, checkpoint, claim)

                self._remove_empty_owned_directory(
                    staging,
                    expected_identity=recovery_tree.root_identity,
                    before_effect=before_empty_rmdir,
                )
            return True
        if self._read_private(marker_path) != self._marker_bytes(intent):
            raise self._fail("materialization recovery marker drifted")
        if manifest_stat is None:
            self._fault_hook("before_marker_only_recovery_admission")
            try:
                with self._pinned_tree(
                    staging,
                    max_files=intent.member_count_limit + 3,
                    max_bytes=self._recovery_scan_max_bytes(intent),
                    allow_empty_directories=True,
                ) as recovery_tree:
                    if recovery_tree.root_identity != self._identity(observed):
                        raise self._fail(
                            "materialization recovery staging identity drifted"
                        )
                    if recovery_tree.read_bytes(
                        marker_file,
                        max_bytes=_MAX_METADATA_BYTES,
                    ) != self._marker_bytes(intent):
                        raise self._fail("materialization recovery marker drifted")
                    if (
                        {item.relative_path for item in recovery_tree.files}
                        == {marker_file}
                        and set(recovery_tree.directory_paths)
                        == {PurePosixPath(".")}
                    ):
                        self._remove_exact_recovery_tree(
                            checkpoint=checkpoint,
                            claim=claim,
                            claim_guard=claim_guard,
                            staging=staging,
                            tree=recovery_tree,
                            last_files=(marker_file,),
                        )
                        return True
            except ParserOutputContractError:
                pass
            self._quarantine_staging(
                checkpoint=checkpoint,
                claim=claim,
                claim_guard=claim_guard,
                intent=intent,
                staging=staging,
                staging_lock=staging_lock,
                expected_identity=self._identity(observed),
            )
            return True
        try:
            with self._pinned_tree(
                staging,
                max_files=intent.member_count_limit + 3,
                max_bytes=self._recovery_scan_max_bytes(intent),
                allow_empty_directories=True,
            ) as recovery_tree:
                if recovery_tree.root_identity != self._identity(observed):
                    raise self._fail(
                        "materialization recovery staging identity drifted"
                    )
                self._validate_recovery_tree_admission(
                    intent=intent,
                    tree=recovery_tree,
                )
                self._remove_admitted_recovery_tree(
                    checkpoint=checkpoint,
                    claim=claim,
                    claim_guard=claim_guard,
                    intent=intent,
                    staging=staging,
                    tree=recovery_tree,
                )
                return True
        except (ParserOutputContractError, ValueError):
            pass
        self._quarantine_staging(
            checkpoint=checkpoint,
            claim=claim,
            claim_guard=claim_guard,
            intent=intent,
            staging=staging,
            staging_lock=staging_lock,
            expected_identity=self._identity(observed),
        )
        return True

    @staticmethod
    def _recovery_scan_max_bytes(intent: MaterializationIntentV4) -> int:
        return (
            max(intent.uncompressed_byte_limit, intent.output_byte_limit)
            + _MAX_METADATA_BYTES
        )

    def _quarantine_staging(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        staging: Path,
        staging_lock: Path,
        expected_identity: tuple[int, int],
    ) -> None:
        self._quarantine_marker_bound_tree(
            checkpoint=checkpoint,
            claim=claim,
            claim_guard=claim_guard,
            intent=intent,
            source=staging,
            staging_lock=staging_lock,
            expected_identity=expected_identity,
            before_phase="before_staging_quarantine_rename",
            after_phase="after_staging_quarantine_rename",
        )

    def _quarantine_marker_bound_tree(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        source: Path,
        staging_lock: Path,
        expected_identity: tuple[int, int],
        before_phase: str,
        after_phase: str,
    ) -> None:
        quarantine = self._quarantine_path(intent)
        if self._try_path_stat(quarantine) is not None:
            source_relpath = source.relative_to(self._root).as_posix()
            quarantine_relpath = quarantine.relative_to(self._root).as_posix()
            raise self._fail(
                "materialization staging quarantine collision: "
                f"source={source_relpath} quarantine={quarantine_relpath}"
            )
        self._require_lock_binding(
            staging_lock,
            "staging",
            self._resource_binding(intent),
        )
        marker = source / PurePosixPath(intent.staging_marker_relpath).name

        def before_quarantine() -> None:
            self._guard(claim_guard, checkpoint, claim)
            self._fault_hook(before_phase)
            self._guard(claim_guard, checkpoint, claim)
            if self._read_private(marker) != self._marker_bytes(intent):
                raise self._fail("materialization recovery marker drifted")

        self._exclusive_rename(
            source,
            quarantine,
            expected_source_identity=expected_identity,
            before_rename=before_quarantine,
            after_rename=lambda: self._fault_hook(after_phase),
        )

    def _quarantine_path(self, intent: MaterializationIntentV4) -> Path:
        staging = PurePosixPath(intent.staging_relpath)
        digest = hashlib.sha256(
            (intent.sha256 + "\x00" + staging.as_posix()).encode("utf-8")
        ).hexdigest()[:32]
        return self._path(
            (staging.parent / f".agent-v4-quarantine-{digest}").as_posix()
        )

    def _remove_admitted_recovery_tree(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        staging: Path,
        tree: PinnedArtifactTree,
    ) -> None:
        self._remove_exact_recovery_tree(
            checkpoint=checkpoint,
            claim=claim,
            claim_guard=claim_guard,
            staging=staging,
            tree=tree,
            last_files=(
                PurePosixPath(intent.output_manifest_relpath),
                PurePosixPath(PurePosixPath(intent.staging_marker_relpath).name),
            ),
        )

    def _remove_exact_recovery_tree(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        staging: Path,
        tree: PinnedArtifactTree,
        last_files: tuple[PurePosixPath, ...],
    ) -> None:
        root_identity = tree.root_identity
        tree.remove_exact_admitted_contents(
            before_effect=lambda: self._guard(claim_guard, checkpoint, claim),
            last_files=last_files,
        )
        self._remove_empty_owned_directory(
            staging,
            expected_identity=root_identity,
            before_effect=lambda: self._guard(claim_guard, checkpoint, claim),
        )

    def _resolve_failed_staging_write(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        staging: Path,
        staging_lock: Path,
        journal: _StagingWriteJournal,
    ) -> None:
        if not journal.mutation_started:
            try:
                self._remove_exact_journaled_staging(
                    checkpoint=checkpoint,
                    claim=claim,
                    claim_guard=claim_guard,
                    intent=intent,
                    staging=staging,
                    journal=journal,
                )
                return
            except ParserOutputContractError:
                # A same-UID injection, replacement, or in-place mutation is
                # not deletion authority.  Preserve the whole marker-bound
                # namespace in the bounded quarantine slot instead.
                pass
        observed = self._try_path_stat(staging)
        if observed is None:
            raise self._fail("failed materialization staging disappeared")
        self._require_owned_dir_stat(observed, "failed materialization staging")
        self._quarantine_staging(
            checkpoint=checkpoint,
            claim=claim,
            claim_guard=claim_guard,
            intent=intent,
            staging=staging,
            staging_lock=staging_lock,
            expected_identity=self._identity(observed),
        )

    def _remove_exact_journaled_staging(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        staging: Path,
        journal: _StagingWriteJournal,
    ) -> None:
        self._fault_hook("before_journaled_staging_cleanup")
        with self._pinned_tree(
            staging,
            max_files=intent.member_count_limit + 1,
            max_bytes=self._recovery_scan_max_bytes(intent),
            allow_empty_directories=True,
        ) as tree:
            if set(tree.directory_paths) != journal.directories:
                raise self._fail("staging journal directory topology drifted")
            observed_files = {item.relative_path: item for item in tree.files}
            if set(observed_files) != set(journal.files):
                raise self._fail("staging journal file topology drifted")
            for relative, expected in journal.files.items():
                item = observed_files[relative]
                identity = item.identity
                observed_identity = _StableFileStat(
                    device=identity.device,
                    inode=identity.inode,
                    mode=identity.mode,
                    uid=identity.uid,
                    link_count=identity.link_count,
                    byte_count=identity.byte_count,
                    modified_ns=identity.modified_ns,
                    changed_ns=identity.changed_ns,
                )
                if (
                    observed_identity != expected.identity
                    or item.sha256 != expected.sha256
                ):
                    raise self._fail("staging journal file identity drifted")
            tree.verify_unchanged()
            self._remove_exact_recovery_tree(
                checkpoint=checkpoint,
                claim=claim,
                claim_guard=claim_guard,
                staging=staging,
                tree=tree,
                last_files=(
                    PurePosixPath(
                        PurePosixPath(intent.staging_marker_relpath).name
                    ),
                ),
            )

    def _remove_empty_owned_directory(
        self,
        path: Path,
        *,
        expected_identity: tuple[int, int],
        before_effect: Callable[[], object],
    ) -> None:
        with self._parent_fd(path) as (parent_fd, name):
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            self._require_owned_dir_stat(observed, "empty owned directory")
            if self._identity(observed) != expected_identity:
                raise self._fail("empty owned directory identity drifted")
            root_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            try:
                if os.listdir(root_fd):
                    raise self._fail("empty owned directory contains foreign entries")
                before_effect()
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                self._require_owned_dir_stat(current, "empty owned directory")
                if (
                    self._identity(current) != expected_identity
                    or self._identity(os.fstat(root_fd)) != expected_identity
                    or os.listdir(root_fd)
                ):
                    raise self._fail("empty owned directory changed before deletion")
                os.rmdir(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(root_fd)

    def cleanup_v4(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        source_checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        intent: MaterializationIntentV4 | None,
        local_receipt: LocalMaterializationReceiptV4 | None,
        plan: LocalCleanupPlanV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        replay_context: V4EvidenceReplayContext,
    ) -> LocalCleanupReceiptV4:
        self._observe_clock()
        validate_v4_cleanup_authorization(
            checkpoint=checkpoint,
            source_checkpoint=source_checkpoint,
            reservation=reservation,
            intent=intent,
            local_receipt=local_receipt,
            plan=plan,
            claim=claim,
            replay_context=replay_context,
        )
        lock_binding = {
            "attempt_id": checkpoint.attempt_id,
            "fence_identity": checkpoint.fence_identity,
        }
        lock_paths = [self._path(reservation.snapshot_lock_relpath)]
        if intent is not None:
            lock_binding = self._resource_binding(intent)
            lock_paths = [
                self._path(intent.spool_lock_relpath),
                self._path(intent.staging_lock_relpath),
            ]
        with self._ordered_locks(lock_paths, lock_binding):
            self._guard(claim_guard, checkpoint, claim)
            volatile_proofs = self._capture_volatile_cleanup_proofs(
                plan=plan,
                reservation=reservation,
                intent=intent,
            )
            results: list[LocalCleanupResourceResultV4] = []
            for resource in plan.resources:
                self._guard(claim_guard, checkpoint, claim)
                source = self._path(resource.relpath)
                if resource.kind == "output":
                    if intent is None or local_receipt is None:
                        raise self._fail("cleanup output lacks materialization evidence")
                    target_candidate = (
                        None
                        if resource.target_relpath is None
                        else self._path(resource.target_relpath)
                    )
                    source_present = self._try_path_stat(source) is not None
                    if resource.action == "transfer":
                        evidence_root = source if source_present else target_candidate
                        if evidence_root is None:
                            raise self._fail("cleanup output evidence disappeared")
                        materialized = self._load_exact_output(
                            intent=intent,
                            output=evidence_root,
                        )
                        if materialized.receipt != local_receipt:
                            raise self._fail("cleanup output receipt drifted")
                if resource.action == "delete":
                    if resource.kind == "staging":
                        if intent is None:
                            raise self._fail(
                                "cleanup staging lacks materialization intent"
                            )
                        self._classify_and_resolve_existing_staging(
                            checkpoint=checkpoint,
                            claim=claim,
                            claim_guard=claim_guard,
                            intent=intent,
                            staging=source,
                            staging_lock=self._path(intent.staging_lock_relpath),
                        )
                        results.append(
                            LocalCleanupResourceResultV4(
                                kind=resource.kind,
                                relpath=resource.relpath,
                                disposition="absent",
                            )
                        )
                        continue
                    self._delete_planned(
                        source,
                        resource.expected_sha256,
                        resource.expected_byte_count,
                        before_effect=lambda: self._guard(
                            claim_guard, checkpoint, claim
                        ),
                        volatile_proof=volatile_proofs.get(
                            (resource.kind, resource.relpath)
                        ),
                        volatile_proof_required=resource.expected_sha256 is None,
                        max_files=(
                            local_receipt.output_file_count
                            if resource.kind == "output" and local_receipt is not None
                            else 1
                        ),
                        last_file_name=(
                            PurePosixPath(intent.staging_marker_relpath).name
                            if resource.kind == "staging" and intent is not None
                            else None
                        ),
                        last_file_bytes=(
                            self._marker_bytes(intent)
                            if resource.kind == "staging" and intent is not None
                            else None
                        ),
                        expected_tree_files=(
                            local_receipt.output_files
                            if resource.kind == "output"
                            and local_receipt is not None
                            else None
                        ),
                    )
                    results.append(
                        LocalCleanupResourceResultV4(
                            kind=resource.kind,
                            relpath=resource.relpath,
                            disposition="absent",
                        )
                    )
                    continue
                assert resource.target_relpath is not None
                target = self._path(resource.target_relpath)
                self._transfer_planned(
                    source=source,
                    target=target,
                    expected_sha256=resource.expected_sha256,
                    expected_byte_count=resource.expected_byte_count,
                    before_effect=lambda: self._guard(
                        claim_guard, checkpoint, claim
                    ),
                    max_files=(
                        local_receipt.output_file_count
                        if local_receipt is not None
                        else 1
                    ),
                )
                results.append(
                    LocalCleanupResourceResultV4(
                        kind=resource.kind,
                        relpath=resource.relpath,
                        disposition="transferred",
                        target_owner_identity=resource.target_owner_identity,
                        target_relpath=resource.target_relpath,
                    )
                )
            return build_local_cleanup_receipt_v4(
                plan=plan,
                cleanup_pending_checkpoint=checkpoint,
                results=tuple(results),
            )

    def promote_or_replay(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        published_relpath: str,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
    ) -> None:
        """Transfer the exact materialized tree before transaction P.

        This is deliberately narrower than cleanup: it neither deletes other
        attempt resources nor advances lifecycle state.  The preparation
        intent owns the target path and the later readiness manifest proves
        the transferred tree before PostgreSQL publication can begin.
        """

        self._observe_clock()
        if (
            type(checkpoint) is not RemoteParseCheckpointV4
            or checkpoint.state != "local_materialized"
            or type(materialized) is not MaterializedProviderDocumentV4
            or type(claim) is not V4ClaimWitness
            or not claim.validates(checkpoint)
            or checkpoint.local_materialization_receipt_sha256
            != materialized.receipt.sha256
            or checkpoint.materialization_intent_sha256
            != materialized.intent.sha256
            or (
                checkpoint.attempt_id,
                checkpoint.fence_identity,
                checkpoint.document_id,
                checkpoint.processing_run_id,
            )
            != (
                materialized.intent.attempt_id,
                materialized.intent.fence_identity,
                materialized.intent.document_id,
                materialized.intent.processing_run_id,
            )
        ):
            raise self._fail("publication output promotion authority drifted")
        validate_relative_resource_path_v4(
            published_relpath,
            "publication parser output",
        )
        intent = materialized.intent
        receipt = materialized.receipt
        source = self._path(intent.output_relpath)
        target = self._path(published_relpath)
        lock_binding = self._resource_binding(intent)
        lock_paths = [
            self._path(intent.spool_lock_relpath),
            self._path(intent.staging_lock_relpath),
        ]
        with self._ordered_locks(lock_paths, lock_binding):
            self._guard(claim_guard, checkpoint, claim)
            evidence_root = (
                source
                if self._try_path_stat(source) is not None
                else target
            )
            loaded = self._load_exact_output(
                intent=intent,
                output=evidence_root,
            )
            if loaded != materialized:
                raise self._fail("publication output promotion evidence drifted")
            self._transfer_planned(
                source=source,
                target=target,
                expected_sha256=receipt.output_files_sha256,
                expected_byte_count=receipt.output_byte_count,
                before_effect=lambda: self._guard(
                    claim_guard,
                    checkpoint,
                    claim,
                ),
                max_files=receipt.output_file_count,
            )
            replayed = self._load_exact_output(
                intent=intent,
                output=target,
            )
            if replayed != materialized:
                raise self._fail("published parser output drifted after promotion")

    def verify_published(
        self,
        *,
        published_relpath: str,
        expected_inventory_sha256: str,
        expected_file_count: int,
        expected_byte_count: int,
    ) -> None:
        """Read-only exact inventory verification for readiness/Doctor/GC."""

        validate_relative_resource_path_v4(
            published_relpath,
            "published parser output",
        )
        if type(expected_file_count) is not int or expected_file_count < 1:
            raise self._fail("published parser output file count is invalid")
        with self._root_lock_coordinator.process_lock:
            root = self._path(published_relpath)
            identity = self._require_tree_inventory(
                root,
                expected_inventory_sha256,
                expected_byte_count,
                max_files=expected_file_count,
            )
            self._fsync_exact_tree_and_parent(
                root,
                expected_sha256=expected_inventory_sha256,
                expected_byte_count=expected_byte_count,
                max_files=expected_file_count,
                expected_identity=identity,
            )

    def acknowledge_v4(
        self,
        *,
        command: ProviderAckCommandV4,
        provider_capability: PrivateProviderCapabilityV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
    ) -> ProviderAckReceiptV4:
        self._observe_clock()
        validate_v4_ack_authorization(
            command=command,
            provider_capability=provider_capability,
            claim=claim,
        )
        intent_value = command.replay_context.evidence_value(
            "materialization_intent", MaterializationIntentV4
        )
        if intent_value is not None and type(intent_value) is not MaterializationIntentV4:
            raise self._fail("ACK materialization intent type drifted")
        intent = cast(MaterializationIntentV4 | None, intent_value)
        if intent is None:
            lock_paths = [
                self._path(command.replay_context.reservation.snapshot_lock_relpath)
            ]
            lock_binding = {
                "attempt_id": command.ack_pending_checkpoint.attempt_id,
                "fence_identity": command.ack_pending_checkpoint.fence_identity,
            }
        else:
            lock_paths = [
                self._path(intent.spool_lock_relpath),
                self._path(intent.staging_lock_relpath),
            ]
            lock_binding = self._resource_binding(intent)
        with self._ordered_locks(lock_paths, lock_binding):
            self._guard(claim_guard, command.ack_pending_checkpoint, claim)
            return self._acknowledge_locked(
                command=command,
                provider_capability=provider_capability,
                claim=claim,
                claim_guard=claim_guard,
            )

    def _acknowledge_locked(
        self,
        *,
        command: ProviderAckCommandV4,
        provider_capability: PrivateProviderCapabilityV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
    ) -> ProviderAckReceiptV4:
        self._guard(claim_guard, command.ack_pending_checkpoint, claim)
        response = self._transport.acknowledge(
            command=command,
            provider_capability=provider_capability,
        )
        if type(response) is not MinerUV4HttpResponse:
            raise self._fail("provider ACK transport returned a forged response")
        if (
            type(response.status_code) is not int
            or type(response.exact_bytes) is not bytes
        ):
            raise self._fail("provider ACK transport returned a forged response")
        status = response.status_code
        response_bytes = response.exact_bytes
        if len(response_bytes) > _MAX_ACK_RESPONSE_BYTES:
            raise self._fail("provider ACK response exceeded its byte limit")
        provider_receipt: str | None = None
        if status == 200:
            value = self._closed_response(
                response_bytes,
                {"schema", "status", "task_id"},
            )
            if (
                value["schema"] != "mineru-task-protocol.v2"
                or value["status"] != "consumed"
                or value["task_id"] != command.remote_task_identity
            ):
                raise self._fail("provider ACK receipt drifted")
            provider_receipt = command.remote_task_identity
        elif status == 204:
            if response_bytes != b"":
                raise self._fail("HTTP 204 ACK carried a response body")
        elif status == 404:
            value = self._closed_response(response_bytes, {"detail"})
            if value != {"detail": "Task not found"}:
                raise self._fail("provider ACK absence is not bound")
        else:
            raise self._fail(f"provider ACK returned HTTP {status}")
        plan = command.cleanup_plan
        return ProviderAckReceiptV4(
            attempt_id=command.ack_pending_checkpoint.attempt_id,
            fence_identity=command.ack_pending_checkpoint.fence_identity,
            document_id=command.ack_pending_checkpoint.document_id,
            processing_run_id=command.ack_pending_checkpoint.processing_run_id,
            outcome=plan.outcome,
            ack_pending_checkpoint_sha256=command.ack_pending_checkpoint.sha256,
            ack_pending_lifecycle_version=command.ack_pending_checkpoint.lifecycle_version,
            accepted_submission_sha256=command.accepted_submission.sha256,
            remote_task_identity=command.remote_task_identity,
            result_owner_identity=command.result_owner_identity,
            terminal_receipt_sha256=plan.terminal_receipt_sha256,
            failure_receipt_sha256=plan.failure_receipt_sha256,
            supersession_receipt_sha256=plan.supersession_receipt_sha256,
            local_materialization_receipt_sha256=(
                plan.local_materialization_receipt_sha256
            ),
            publication_winner_sha256=plan.publication_winner_sha256,
            cleanup_plan_sha256=plan.sha256,
            cleanup_receipt_sha256=command.cleanup_receipt.sha256,
            provider_protocol_version=command.provider_protocol_version,
            request_identity=command.request_identity,
            ack_request_sha256=command.ack_request_sha256,
            ack_kind="absent" if status == 404 else "consumed",
            http_status=status,
            provider_response_sha256=self._digest(response_bytes),
            provider_response_byte_count=len(response_bytes),
            provider_receipt_identity=provider_receipt,
        )

    def _ensure_spool(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        accepted: AcceptedSubmissionReceiptV4,
        capability: PrivateProviderCapabilityV4,
        spool: Path,
        part: Path,
        owner: Path,
    ) -> tuple[int, int]:
        owner_bytes = self._spool_owner_bytes(intent)
        if spool.exists() or spool.is_symlink():
            spool_stat = self._try_path_stat(spool)
            if spool_stat is None:
                raise self._fail("final spool disappeared")
            self._require_owned_regular_stat(spool_stat, "final spool")
            spool_identity = self._identity(spool_stat)
            if part.exists() or part.is_symlink():
                raise self._fail("final spool coexists with a partial file")
            if owner.exists() or owner.is_symlink():
                if self._read_private(owner) != owner_bytes:
                    raise self._fail("final spool owner metadata drifted")
                self._remove_owned_file(
                    owner,
                    allow_absent=False,
                    before_effect=lambda: self._guard(
                        claim_guard, checkpoint, claim
                    ),
                )
            self._fsync_dir(spool.parent)
            return spool_identity
        if part.exists() or part.is_symlink() or owner.exists() or owner.is_symlink():
            if not owner.exists() or owner.is_symlink() or self._read_private(owner) != owner_bytes:
                raise self._fail("spool partial ownership collision")
            self._guard(claim_guard, checkpoint, claim)
            def before() -> None:
                self._guard(claim_guard, checkpoint, claim)

            self._remove_owned_file(part, allow_absent=True, before_effect=before)
            self._remove_owned_file(owner, allow_absent=False, before_effect=before)
            self._fsync_dir(part.parent)
        self._ensure_parent(part)
        self._write_private(owner, owner_bytes)
        digest = hashlib.sha256()
        byte_count = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        with self._parent_fd(part, create=True) as (parent_fd, name):
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            part_stat = os.fstat(fd)
            self._require_owned_regular_stat(part_stat, "new spool part")
            self._require_entry_identity(parent_fd, name, part_stat, "new spool part")
            part_identity = self._identity(part_stat)
        try:
            for chunk in self._transport.stream_result(
                accepted_submission=accepted,
                provider_capability=capability,
            ):
                if type(chunk) is not bytes or not chunk:
                    raise self._fail("provider result stream yielded an invalid chunk")
                byte_count += len(chunk)
                if byte_count > intent.result_byte_limit:
                    raise self._fail("provider result exceeded its byte limit")
                digest.update(chunk)
                self._write_all(fd, chunk)
            os.fsync(fd)
            if self._identity(os.fstat(fd)) != part_identity:
                raise self._fail("spool part changed while downloading")
        finally:
            os.close(fd)
        if byte_count != intent.artifact_byte_count or (
            "sha256:" + digest.hexdigest()
        ) != intent.artifact_sha256:
            raise self._fail("provider result identity drifted")
        self._fault_hook("after_spool_fsync")
        self._guard(claim_guard, checkpoint, claim)
        self._exclusive_rename(
            part,
            spool,
            expected_source_identity=part_identity,
        )
        self._fault_hook("after_spool_rename")
        self._fsync_dir(spool.parent)
        self._remove_owned_file(
            owner,
            allow_absent=False,
            before_effect=lambda: self._guard(claim_guard, checkpoint, claim),
        )
        self._fsync_dir(owner.parent)
        return part_identity

    def _prepare_staging(
        self,
        *,
        intent: MaterializationIntentV4,
        staging: Path,
        marker: Path,
        journal: _StagingWriteJournal,
    ) -> None:
        if staging.exists() or staging.is_symlink():
            raise self._fail("materialization staging classifier left a collision")
        self._ensure_parent(staging)
        self._mkdir_exact(staging)
        journal.add_directory(PurePosixPath("."))
        self._fault_hook("after_staging_mkdir")
        marker_bytes = self._marker_bytes(intent)
        marker_stat = self._write_private(marker, marker_bytes)
        journal.add_file(
            PurePosixPath(marker.relative_to(staging).as_posix()),
            observed=marker_stat,
            sha256=self._digest(marker_bytes),
        )
        self._fsync_dir(staging)

    def _materialize_staging(
        self,
        *,
        intent: MaterializationIntentV4,
        archive: zipfile.ZipFile,
        zip_metadata: _ValidatedZipMetadata,
        verify_spool: Callable[[], None],
        staging: Path,
        marker: Path,
        journal: _StagingWriteJournal,
        before_destructive: Callable[[], None],
    ) -> LocalMaterializationObservationsV4:
        unpack = staging / ".unpack"
        self._mkdir_exact(unpack)
        journal.add_directory(PurePosixPath(".unpack"))
        member_count, uncompressed_bytes = self._extract_zip(
            archive=archive,
            metadata=zip_metadata,
            output=unpack,
            intent=intent,
            verify_spool=verify_spool,
            staging=staging,
            journal=journal,
        )
        self._fault_hook("after_zip_extract")
        with self._pinned_tree(
            unpack,
            max_files=intent.member_count_limit,
            max_bytes=intent.uncompressed_byte_limit,
        ) as unpack_tree:
            initial_read = self._reader.read_pinned(
                unpack_tree,
                source_pdf_sha256=intent.source_pdf_sha256,
            )
            artifact_root = unpack.joinpath(*initial_read.artifact_root_relpath.parts)
            unpack_tree.verify_unchanged()
        with self._pinned_tree(
            artifact_root,
            max_files=intent.member_count_limit,
            max_bytes=intent.uncompressed_byte_limit,
        ) as tree:
            read = self._reader.read_pinned(
                tree,
                source_pdf_sha256=intent.source_pdf_sha256,
            )
            if read.artifact_root_relpath != PurePosixPath("."):
                raise self._fail("flattened MinerU artifact root drifted")
            tree.verify_unchanged()
            document = read.document
        if len(document.pages) != intent.source_page_count:
            raise self._fail("MinerU output page count drifted")
        context = intent.provider_envelope_context
        envelope = ProviderDocumentEnvelope.build(
            document_id=context.document_id,
            artifact_owner_processing_run_id=context.processing_run_id,
            provider=context.provider,
            provider_document_id=context.provider_document_id,
            source_pdf_relpath=context.source_pdf_relpath,
            source_pdf_page_count=context.source_page_count,
            parser_artifact_root_relpath=context.parser_artifact_root_relpath,
            parser_target_identity=context.parser_target_identity,
            provider_document=document,
        )
        envelope_bytes = provider_document_envelope_to_bytes(envelope)
        parser_files = tuple(
            LocalMaterializationPayloadFileV4(
                role="parser_artifact",
                relpath=item.relative_path,
                sha256=item.sha256,
                byte_count=item.size_bytes,
            )
            for item in document.artifacts
        )
        envelope_file = LocalMaterializationPayloadFileV4(
            role="provider_envelope",
            relpath=intent.provider_envelope_relpath,
            sha256=self._digest(envelope_bytes),
            byte_count=len(envelope_bytes),
        )
        payload_files = tuple(
            sorted((*parser_files, envelope_file), key=lambda item: item.relpath)
        )
        payload_bytes = sum(item.byte_count for item in payload_files)
        decoded_bytes = sum(item.byte_count for item in parser_files)
        marker_bytes = len(self._read_private(marker))
        if (
            decoded_bytes > intent.decoded_byte_limit
            or payload_bytes > intent.output_byte_limit
        ):
            raise self._fail("materialization exceeded its resource envelope")
        temp_peak = intent.artifact_byte_count + max(
            uncompressed_bytes + marker_bytes,
            decoded_bytes + len(envelope_bytes) + marker_bytes,
        )
        for _ in range(4):
            observations = LocalMaterializationObservationsV4(
                member_count=member_count,
                uncompressed_byte_count=uncompressed_bytes,
                decoded_byte_count=decoded_bytes,
                temporary_disk_peak_byte_count=temp_peak,
                output_file_count=len(payload_files),
                output_byte_count=payload_bytes,
            )
            manifest = seal_local_materialization_manifest_v4(
                attempt_id=intent.attempt_id,
                fence_identity=intent.fence_identity,
                document_id=intent.document_id,
                processing_run_id=intent.processing_run_id,
                materialization_intent_sha256=intent.sha256,
                terminal_receipt_sha256=intent.terminal_receipt_sha256,
                remote_task_identity=intent.remote_task_identity,
                artifact_owner_identity=intent.artifact_owner_identity,
                artifact_sha256=intent.artifact_sha256,
                artifact_byte_count=intent.artifact_byte_count,
                source_pdf_sha256=intent.source_pdf_sha256,
                source_page_count=intent.source_page_count,
                parser_target_sha256=intent.parser_target_sha256,
                spool_relpath=intent.spool_relpath,
                output_relpath=intent.output_relpath,
                provider_envelope_relpath=intent.provider_envelope_relpath,
                provider_envelope_sha256=envelope_file.sha256,
                provider_envelope_byte_count=envelope_file.byte_count,
                observations=observations,
                payload_files=payload_files,
            )
            exact_peak = intent.artifact_byte_count + max(
                uncompressed_bytes + marker_bytes,
                decoded_bytes
                + len(envelope_bytes)
                + len(manifest.canonical_bytes)
                + marker_bytes,
            )
            if exact_peak == temp_peak:
                break
            temp_peak = exact_peak
        else:
            raise self._fail("materialization peak observation did not stabilize")
        if (
            temp_peak > intent.temporary_disk_byte_limit
            or payload_bytes + len(manifest.canonical_bytes)
            > intent.output_byte_limit
        ):
            raise self._fail("materialization exceeded its final byte envelope")
        journal.mutation_started = True
        self._fault_hook("before_flatten")
        self._flatten_artifact_root(
            artifact_root=artifact_root,
            staging=staging,
            before_effect=before_destructive,
        )
        artifact_relative = PurePosixPath(artifact_root.relative_to(unpack).as_posix())
        expected_unpack_directories = {PurePosixPath(".")}
        current = PurePosixPath(".")
        for part in artifact_relative.parts:
            current = current / part
            expected_unpack_directories.add(current)
        with self._pinned_tree(
            unpack,
            max_files=intent.member_count_limit,
            max_bytes=intent.uncompressed_byte_limit,
            allow_empty_directories=True,
        ) as unpack_cleanup:
            if unpack_cleanup.files or set(
                unpack_cleanup.directory_paths
            ) != expected_unpack_directories:
                raise self._fail("unpack cleanup tree is not the exact live projection")
            unpack_identity = unpack_cleanup.root_identity
            unpack_cleanup.remove_exact_admitted_contents(
                before_effect=before_destructive,
                last_files=(),
            )
            self._remove_empty_owned_directory(
                unpack,
                expected_identity=unpack_identity,
                before_effect=before_destructive,
            )
        envelope_path = staging / intent.provider_envelope_relpath
        self._write_private(envelope_path, envelope_bytes)
        self._write_private(
            staging / LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
            manifest.canonical_bytes,
        )
        self._fsync_tree(staging)
        return observations

    def _finish_promoted_marker(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        intent: MaterializationIntentV4,
        output: Path,
        expected_output_identity: tuple[int, int],
    ) -> None:
        # Replaying a rename which may have crashed before the directory fsync
        # must make the published directory entry durable before issuing a
        # receipt, even when the marker was already removed.
        if self._path_identity(output) != expected_output_identity:
            raise self._fail("promoted materialization output was replaced")
        self._fsync_dir(output.parent)
        self._fsync_dir(output)
        marker = output / PurePosixPath(intent.staging_marker_relpath).name
        if not marker.exists() and not marker.is_symlink():
            return
        if self._read_private(marker) != self._marker_bytes(intent):
            raise self._fail("promoted materialization marker drifted")
        if self._path_identity(output) != expected_output_identity:
            raise self._fail("promoted materialization output was replaced")
        self._remove_owned_file(
            marker,
            allow_absent=False,
            before_effect=lambda: self._guard(claim_guard, checkpoint, claim),
        )
        self._fsync_dir(output)

    def _load_exact_output(
        self,
        *,
        intent: MaterializationIntentV4,
        output: Path,
        allow_marker: bool = False,
        expected_output_identity: tuple[int, int] | None = None,
        fsync_exact: bool = False,
    ) -> MaterializedProviderDocumentV4:
        if output.is_symlink() or not output.is_dir():
            raise self._fail("materialization output is not an owned directory")
        with self._pinned_tree(
            output,
            max_files=intent.member_count_limit + 3,
            max_bytes=intent.output_byte_limit + _MAX_METADATA_BYTES,
        ) as tree:
            if (
                expected_output_identity is not None
                and tree.root_identity != expected_output_identity
            ):
                raise self._fail("materialization output identity drifted")
            value = self._materialized_from_tree(
                intent=intent,
                tree=tree,
                allow_marker=allow_marker,
                staging_absent=True,
            )
            if fsync_exact:
                tree.fsync_exact()
            tree.verify_unchanged()
            return value

    def _materialized_from_tree(
        self,
        *,
        intent: MaterializationIntentV4,
        tree: PinnedArtifactTree,
        allow_marker: bool,
        staging_absent: bool,
    ) -> MaterializedProviderDocumentV4:
        marker_relpath = PurePosixPath(intent.staging_marker_relpath).name
        marker_file = PurePosixPath(marker_relpath)
        marker_present = tree.has_file(marker_file)
        if marker_present:
            if not allow_marker or tree.read_bytes(
                marker_file, max_bytes=_MAX_METADATA_BYTES
            ) != self._marker_bytes(intent):
                raise self._fail("promoted materialization marker drifted")
        manifest_file = tree.require_file(
            PurePosixPath(LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME)
        )
        envelope_file = tree.require_file(PurePosixPath(intent.provider_envelope_relpath))
        manifest = decode_local_materialization_manifest_v4(
            tree.read_bytes(
                manifest_file.relative_path,
                max_bytes=intent.output_byte_limit,
            )
        )
        envelope = provider_document_envelope_from_bytes(
            tree.read_bytes(
                envelope_file.relative_path,
                max_bytes=intent.output_byte_limit,
            )
        )
        parser_files = tuple(
            (
                item.relative_path.as_posix(),
                item.sha256,
                item.size_bytes,
            )
            for item in tree.files
            if item.relative_path.as_posix()
            not in {
                intent.provider_envelope_relpath,
                intent.output_manifest_relpath,
                marker_relpath,
            }
        )
        document_files = tuple(
            (item.relative_path, item.sha256, item.size_bytes)
            for item in envelope.provider_document.artifacts
        )
        if parser_files != document_files:
            raise self._fail("materialized MinerU tree drifted from envelope")
        files = tuple(
            LocalOutputFileV4(
                relpath=item.relative_path.as_posix(),
                sha256=item.sha256,
                byte_count=item.size_bytes,
            )
            for item in tree.files
            if item.relative_path != marker_file
        )
        tree.verify_unchanged()
        receipt = build_local_materialization_receipt_v4(
            intent=intent,
            manifest=manifest,
            source_page_count=intent.source_page_count,
            output_files=files,
            provider_envelope_relpath=intent.provider_envelope_relpath,
            output_manifest_relpath=intent.output_manifest_relpath,
            member_count=manifest.observations.member_count,
            uncompressed_byte_count=manifest.observations.uncompressed_byte_count,
            decoded_byte_count=manifest.observations.decoded_byte_count,
            temporary_disk_peak_byte_count=manifest.observations.temporary_disk_peak_byte_count,
            file_fsync_completed=True,
            output_parent_fsync_completed=True,
            # A marker-bearing tree is validated as the exact future published
            # projection; callers never return this receipt until marker-last
            # removal has completed and been fsynced.
            marker_removed=True,
            spool_part_absent=(
                self._try_path_stat(self._path(intent.spool_part_relpath)) is None
            ),
            spool_part_owner_absent=(
                self._try_path_stat(self._path(intent.spool_part_owner_relpath))
                is None
            ),
            staging_absent=staging_absent,
        )
        return MaterializedProviderDocumentV4(
            receipt=receipt,
            intent=intent,
            provider_envelope=envelope,
            manifest=manifest,
        )

    @contextmanager
    def _preflighted_provider_zip(
        self,
        *,
        spool: Path,
        intent: MaterializationIntentV4,
        expected_identity: tuple[int, int],
    ) -> Iterator[
        tuple[
            zipfile.ZipFile,
            _ValidatedZipMetadata,
            Callable[[], None],
        ]
    ]:
        with self._parent_fd(spool) as (parent_fd, name):
            self._assert_dir_path_identity(spool.parent, parent_fd)
            try:
                spool_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise self._fail("provider result ZIP is absent or unsafe") from exc
            try:
                observed = os.fstat(spool_fd)
                stable_before_hash = _StableFileStat.from_stat(observed)
                self._require_owned_regular_stat(observed, "provider result ZIP")
                if self._identity(observed) != expected_identity:
                    raise self._fail("provider result ZIP identity drifted")
                self._require_entry_identity(
                    parent_fd,
                    name,
                    observed,
                    "provider result ZIP",
                )
                digest, byte_count = self._hash_fd(spool_fd)
                if (
                    digest != intent.artifact_sha256
                    or byte_count != intent.artifact_byte_count
                ):
                    raise self._fail("provider result ZIP identity drifted")
                after_hash = os.fstat(spool_fd)
                stable = _StableFileStat.from_stat(after_hash)
                if stable != stable_before_hash:
                    raise self._fail("provider result ZIP changed while hashing")
                self._require_entry_identity(
                    parent_fd,
                    name,
                    after_hash,
                    "provider result ZIP",
                )
                self._assert_dir_path_identity(spool.parent, parent_fd)
                os.lseek(spool_fd, 0, os.SEEK_SET)
            except BaseException:
                os.close(spool_fd)
                raise

            def verify_spool() -> None:
                self._assert_dir_path_identity(spool.parent, parent_fd)
                current = os.fstat(spool_fd)
                if _StableFileStat.from_stat(current) != stable:
                    raise self._fail(
                        "provider result ZIP changed during materialization"
                    )
                self._require_entry_identity(
                    parent_fd,
                    name,
                    current,
                    "provider result ZIP",
                )
                self._assert_dir_path_identity(spool.parent, parent_fd)

            spool_file = os.fdopen(spool_fd, "rb", closefd=True)
            try:
                archive = zipfile.ZipFile(spool_file)
            except (OSError, zipfile.BadZipFile) as exc:
                spool_file.close()
                raise self._fail("provider result is not a valid ZIP") from exc
            with spool_file, archive:
                metadata = self._validate_zip_metadata(
                    archive=archive,
                    intent=intent,
                )
                self._fault_hook("after_zip_preflight")
                verify_spool()
                yield archive, metadata, verify_spool

    def _validate_zip_metadata(
        self,
        *,
        archive: zipfile.ZipFile,
        intent: MaterializationIntentV4,
    ) -> _ValidatedZipMetadata:
        infos = archive.infolist()
        if not infos or len(infos) > intent.member_count_limit:
            raise self._fail("ZIP member count is outside its envelope")
        normalized: list[_ValidatedZipMember] = []
        path_index = _ZipPathCollisionIndex.empty()
        uncompressed = 0
        for info in infos:
            if (
                type(info.filename) is not str
                or type(info.orig_filename) is not str
                or "\x00" in info.filename
                or "\x00" in info.orig_filename
                or info.orig_filename != info.filename
            ):
                raise self._fail("ZIP member name evidence is not exact")
            name = info.filename[:-1] if info.is_dir() else info.filename
            validate_relative_resource_path_v4(name, "MinerU ZIP member")
            if name != unicodedata.normalize("NFC", name) or "\\" in name:
                raise self._fail("ZIP member path is not portable")
            member_parts = PurePosixPath(name).parts
            if len(member_parts) > _MAX_RECOVERY_PATH_PARTS:
                raise self._fail("ZIP member path exceeds recovery depth")
            key = tuple(part.casefold() for part in member_parts)
            is_directory = info.is_dir()
            if not path_index.admit(key, is_directory=is_directory):
                raise self._fail("ZIP members collide or overlap")
            mode = info.external_attr >> 16
            if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise self._fail("ZIP contains a non-regular member")
            if info.file_size < 0:
                raise self._fail("ZIP member size is invalid")
            if not info.is_dir():
                uncompressed += info.file_size
                if uncompressed > intent.uncompressed_byte_limit:
                    raise self._fail("ZIP exceeded its uncompressed byte limit")
            normalized.append(
                _ValidatedZipMember.from_info(
                    info,
                    normalized_name=name,
                )
            )
        if intent.artifact_byte_count + uncompressed > intent.temporary_disk_byte_limit:
            raise self._fail("ZIP exceeds its temporary-disk envelope")
        return _ValidatedZipMetadata(
            members=tuple(normalized),
            uncompressed_byte_count=uncompressed,
        )

    def _extract_zip(
        self,
        *,
        archive: zipfile.ZipFile,
        metadata: _ValidatedZipMetadata,
        output: Path,
        intent: MaterializationIntentV4,
        verify_spool: Callable[[], None],
        staging: Path,
        journal: _StagingWriteJournal,
    ) -> tuple[int, int]:
        verify_spool()
        written = 0
        for member in metadata.members:
            try:
                info = archive.getinfo(member.archive_name)
            except KeyError as exc:
                raise self._fail("ZIP member metadata drifted") from exc
            if not member.validates(info):
                raise self._fail("ZIP member metadata drifted")
            name = member.normalized_name
            target = output.joinpath(*PurePosixPath(name).parts)
            if member.is_directory:
                self._ensure_dirs_beneath(
                    output,
                    target,
                    journal_root=staging,
                    journal=journal,
                )
                continue
            self._ensure_dirs_beneath(
                output,
                target.parent,
                journal_root=staging,
                journal=journal,
            )
            with self._parent_fd(target, create=True) as (parent_fd, basename):
                fd = os.open(
                    basename,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                member_digest = hashlib.sha256()
                member_relative = PurePosixPath(target.relative_to(staging).as_posix())

                def record_member() -> os.stat_result:
                    os.fsync(fd)
                    member_stat = os.fstat(fd)
                    self._require_owned_regular_stat(
                        member_stat,
                        "extracted ZIP member",
                    )
                    self._require_entry_identity(
                        parent_fd,
                        basename,
                        member_stat,
                        "extracted ZIP member",
                    )
                    journal.add_file(
                        member_relative,
                        observed=member_stat,
                        sha256="sha256:" + member_digest.hexdigest(),
                    )
                    return member_stat

                try:
                    try:
                        with archive.open(info, "r") as source:
                            while chunk := source.read(_CHUNK_BYTES):
                                written += len(chunk)
                                member_digest.update(chunk)
                                self._write_all(fd, chunk)
                                if written > intent.uncompressed_byte_limit:
                                    raise self._fail(
                                        "ZIP stream exceeded its byte limit"
                                    )
                    except (
                        ParserOutputContractError,
                        zipfile.BadZipFile,
                        RuntimeError,
                        EOFError,
                        zlib.error,
                    ) as exc:
                        record_member()
                        if isinstance(exc, ParserOutputContractError):
                            raise
                        raise self._fail("ZIP member content is invalid") from exc
                    member_stat = record_member()
                    if member_stat.st_size != member.file_size:
                        raise self._fail("ZIP member size drifted while extracting")
                finally:
                    os.close(fd)
        self._fsync_tree(output)
        verify_spool()
        if written != metadata.uncompressed_byte_count:
            raise self._fail("ZIP uncompressed byte count drifted while extracting")
        return len(metadata.members), written

    def _flatten_artifact_root(
        self,
        *,
        artifact_root: Path,
        staging: Path,
        before_effect: Callable[[], None],
    ) -> None:
        if artifact_root == staging or staging not in artifact_root.parents:
            raise self._fail("located MinerU artifact root escaped staging")
        with self._open_dir(artifact_root) as artifact_fd:
            child_names = tuple(os.listdir(artifact_fd))
        for name in child_names:
            validate_relative_resource_path_v4(name, "MinerU artifact root member")
            child = artifact_root / name
            target = staging / child.name
            if target.exists() or target.is_symlink():
                raise self._fail("flattened MinerU artifact collision")
            source_identity = self._path_identity(child)
            before_effect()
            self._exclusive_rename(
                child,
                target,
                expected_source_identity=source_identity,
            )
        self._fsync_dir(staging)

    def _transfer_planned(
        self,
        *,
        source: Path,
        target: Path,
        expected_sha256: str | None,
        expected_byte_count: int | None,
        before_effect: Callable[[], None],
        max_files: int,
    ) -> None:
        if self._try_path_stat(target) is not None:
            target_identity = self._require_tree_inventory(
                target,
                expected_sha256,
                expected_byte_count,
                max_files=max_files,
            )
            if self._try_path_stat(source) is not None:
                raise self._fail("cleanup transfer has both source and target")
            self._fsync_exact_tree_and_parent(
                target,
                expected_sha256=expected_sha256,
                expected_byte_count=expected_byte_count,
                max_files=max_files,
                expected_identity=target_identity,
            )
            return
        if self._try_path_stat(source) is None:
            raise self._fail("cleanup transfer lost both source and target")
        source_identity = self._require_tree_inventory(
            source,
            expected_sha256,
            expected_byte_count,
            max_files=max_files,
        )
        self._ensure_parent(target)
        with self._open_dir(source.parent) as source_parent_fd:
            source_device = os.fstat(source_parent_fd).st_dev
        with self._open_dir(target.parent) as target_parent_fd:
            target_device = os.fstat(target_parent_fd).st_dev
        if source_device != target_device:
            raise self._fail("cleanup transfer crosses filesystems")
        self._fault_hook("before_cleanup_transfer")
        if (
            self._require_tree_inventory(
                source,
                expected_sha256,
                expected_byte_count,
                max_files=max_files,
            )
            != source_identity
        ):
            raise self._fail("cleanup transfer source was replaced")

        def before_transfer_rename() -> None:
            before_effect()
            if (
                self._require_tree_inventory(
                    source,
                    expected_sha256,
                    expected_byte_count,
                    max_files=max_files,
                )
                != source_identity
            ):
                raise self._fail("cleanup transfer source was replaced")

        self._exclusive_rename(
            source,
            target,
            expected_source_identity=source_identity,
            before_rename=before_transfer_rename,
            after_rename=lambda: self._fault_hook(
                "after_cleanup_transfer_rename"
            ),
        )
        if (
            self._require_tree_inventory(
                target,
                expected_sha256,
                expected_byte_count,
                max_files=max_files,
            )
            != source_identity
        ):
            raise self._fail("cleanup transfer target was replaced")

    def _delete_planned(
        self,
        path: Path,
        expected_sha256: str | None,
        expected_byte_count: int | None,
        before_effect: Callable[[], None],
        volatile_proof: tuple[int, int] | None,
        volatile_proof_required: bool,
        max_files: int,
        last_file_name: str | None,
        last_file_bytes: bytes | None,
        expected_tree_files: tuple[LocalOutputFileV4, ...] | None,
    ) -> None:
        observed = self._try_path_stat(path)
        if observed is None:
            if self._try_path_stat(path.parent) is not None:
                self._fsync_dir(path.parent)
            return
        if volatile_proof_required and volatile_proof is None:
            raise self._fail("volatile cleanup resource lacks exact ownership proof")
        if stat.S_ISLNK(observed.st_mode):
            raise self._fail("cleanup resource is a symlink")
        if expected_sha256 is None:
            if stat.S_ISDIR(observed.st_mode):
                self._require_owned_dir_stat(observed, "cleanup volatile tree")
            else:
                self._require_owned_regular_stat(observed, "cleanup volatile file")
            expected_identity = self._identity(observed)
            if expected_identity != volatile_proof:
                raise self._fail("volatile cleanup ownership proof drifted")
        elif stat.S_ISDIR(observed.st_mode):
            expected_identity = self._identity(observed)
        else:
            assert expected_byte_count is not None
            expected_identity = self._require_file(
                path, expected_sha256, expected_byte_count
            )
        self._fault_hook("before_cleanup_delete")
        if stat.S_ISDIR(observed.st_mode):
            with self._pinned_tree(
                path,
                max_files=max_files,
                max_bytes=(
                    expected_byte_count
                    if expected_byte_count is not None
                    else _MAX_METADATA_BYTES
                ),
                allow_empty_directories=(
                    expected_sha256 is None or expected_tree_files is not None
                ),
            ) as tree:
                if tree.root_identity != expected_identity:
                    raise self._fail("cleanup tree identity drifted")
                last_files: tuple[PurePosixPath, ...] = ()
                if expected_sha256 is not None:
                    assert expected_byte_count is not None
                    if expected_tree_files is None:
                        self._validate_tree_inventory(
                            tree,
                            expected_sha256=expected_sha256,
                            expected_byte_count=expected_byte_count,
                        )
                    else:
                        self._validate_exact_cleanup_projection(
                            tree=tree,
                            expected_files={
                                PurePosixPath(item.relpath): (
                                    item.sha256,
                                    item.byte_count,
                                )
                                for item in expected_tree_files
                            },
                            last_files=(),
                            require_full=False,
                        )
                else:
                    if last_file_name is None:
                        raise self._fail(
                            "volatile cleanup tree lacks an exact proof file"
                        )
                    marker_file = PurePosixPath(last_file_name)
                    observed_files = {
                        item.relative_path for item in tree.files
                    }
                    if observed_files == {marker_file}:
                        if tree.read_bytes(
                            marker_file,
                            max_bytes=_MAX_METADATA_BYTES,
                        ) != last_file_bytes:
                            raise self._fail(
                                "staging cleanup marker proof drifted"
                            )
                        last_files = (marker_file,)
                    elif observed_files:
                        raise self._fail(
                            "staging cleanup tree is ambiguous"
                        )
                    if set(tree.directory_paths) != {PurePosixPath(".")}:
                        raise self._fail(
                            "staging cleanup tree is ambiguous"
                        )
                root_identity = tree.root_identity
                self._fault_hook("before_cleanup_exact_delete")
                tree.remove_exact_admitted_contents(
                    before_effect=before_effect,
                    last_files=last_files,
                )
                self._remove_empty_owned_directory(
                    path,
                    expected_identity=root_identity,
                    before_effect=before_effect,
                )
        else:
            self._remove_owned_file(
                path,
                allow_absent=False,
                before_effect=before_effect,
                expected_identity=expected_identity,
            )

    def _capture_volatile_cleanup_proofs(
        self,
        *,
        plan: LocalCleanupPlanV4,
        reservation: ResourceReservationV4,
        intent: MaterializationIntentV4 | None,
    ) -> dict[tuple[str, str], tuple[int, int] | None]:
        proofs: dict[tuple[str, str], tuple[int, int] | None] = {}
        volatile = {
            (resource.kind, resource.relpath)
            for resource in plan.resources
            if resource.expected_sha256 is None
        }
        snapshot_keys = {
            ("snapshot_part", reservation.snapshot_part_relpath),
            ("snapshot_part_owner", reservation.snapshot_part_owner_relpath),
        }
        if volatile & snapshot_keys:
            snapshot_part = self._path(reservation.snapshot_part_relpath)
            snapshot_owner = self._path(reservation.snapshot_part_owner_relpath)
            part_stat = self._try_path_stat(snapshot_part)
            owner_stat = self._try_path_stat(snapshot_owner)
            if part_stat is not None or owner_stat is not None:
                raise self._fail(
                    "snapshot partial cleanup lacks a canonical writer proof"
                )
            for key in volatile & snapshot_keys:
                proofs[key] = None
        if intent is None:
            return proofs
        spool_keys = {
            ("spool_part", intent.spool_part_relpath),
            ("spool_part_owner", intent.spool_part_owner_relpath),
        }
        if volatile & spool_keys:
            part = self._path(intent.spool_part_relpath)
            owner = self._path(intent.spool_part_owner_relpath)
            part_stat = self._try_path_stat(part)
            owner_stat = self._try_path_stat(owner)
            if part_stat is not None and owner_stat is None:
                raise self._fail("spool partial lacks its canonical owner proof")
            if owner_stat is not None:
                if self._read_private(owner) != self._spool_owner_bytes(intent):
                    raise self._fail("spool partial owner proof drifted")
                proofs[("spool_part_owner", intent.spool_part_owner_relpath)] = (
                    self._identity(owner_stat)
                )
            else:
                proofs[("spool_part_owner", intent.spool_part_owner_relpath)] = None
            if part_stat is not None:
                self._require_owned_regular_stat(part_stat, "spool partial")
                proofs[("spool_part", intent.spool_part_relpath)] = self._identity(
                    part_stat
                )
            else:
                proofs[("spool_part", intent.spool_part_relpath)] = None
        staging_keys = {
            ("staging", intent.staging_relpath),
            ("staging_marker", intent.staging_marker_relpath),
        }
        if volatile & staging_keys:
            staging = self._path(intent.staging_relpath)
            marker = self._path(intent.staging_marker_relpath)
            staging_stat = self._try_path_stat(staging)
            marker_stat = self._try_path_stat(marker)
            if staging_stat is None:
                if marker_stat is not None:
                    raise self._fail("staging marker escaped its staging namespace")
                proofs[("staging", intent.staging_relpath)] = None
                proofs[("staging_marker", intent.staging_marker_relpath)] = None
            else:
                self._require_owned_dir_stat(staging_stat, "staging cleanup tree")
                if marker_stat is None:
                    if not self._directory_is_empty(staging):
                        raise self._fail("staging cleanup lacks its canonical marker")
                    proofs[("staging_marker", intent.staging_marker_relpath)] = None
                else:
                    if self._read_private(marker) != self._marker_bytes(intent):
                        raise self._fail("staging cleanup marker proof drifted")
                    proofs[("staging_marker", intent.staging_marker_relpath)] = (
                        self._identity(marker_stat)
                    )
                proofs[("staging", intent.staging_relpath)] = self._identity(
                    staging_stat
                )
        return {key: value for key, value in proofs.items() if key in volatile}

    def _require_tree_inventory(
        self,
        path: Path,
        expected_sha256: str | None,
        expected_byte_count: int | None,
        *,
        max_files: int,
    ) -> tuple[int, int]:
        if expected_sha256 is None or expected_byte_count is None:
            raise self._fail("cleanup durable tree lacks exact identity")
        if path.is_symlink() or not path.is_dir():
            raise self._fail("cleanup output is not an owned directory")
        with self._pinned_tree(
            path,
            max_files=max_files,
            max_bytes=expected_byte_count,
        ) as tree:
            self._validate_tree_inventory(
                tree,
                expected_sha256=expected_sha256,
                expected_byte_count=expected_byte_count,
            )
            tree.verify_unchanged()
            return tree.root_identity

    def _validate_tree_inventory(
        self,
        tree: PinnedArtifactTree,
        *,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> None:
        files = tuple(
            LocalOutputFileV4(
                relpath=item.relative_path.as_posix(),
                sha256=item.sha256,
                byte_count=item.size_bytes,
            )
            for item in tree.files
        )
        if (
            local_output_files_sha256_v4(files) != expected_sha256
            or sum(item.byte_count for item in files) != expected_byte_count
        ):
            raise self._fail("cleanup output inventory drifted")

    def _fsync_exact_tree_and_parent(
        self,
        path: Path,
        *,
        expected_sha256: str | None,
        expected_byte_count: int | None,
        max_files: int,
        expected_identity: tuple[int, int],
    ) -> None:
        with self._pinned_tree(
            path,
            max_files=max_files,
            max_bytes=cast(int, expected_byte_count),
        ) as tree:
            if tree.root_identity != expected_identity:
                raise self._fail("cleanup transfer target identity drifted")
            self._validate_tree_inventory(
                tree,
                expected_sha256=cast(str, expected_sha256),
                expected_byte_count=cast(int, expected_byte_count),
            )
            tree.fsync_exact()
            tree.verify_unchanged()
        with self._parent_fd(path) as (parent_fd, name):
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if self._identity(current) != expected_identity:
                raise self._fail("cleanup transfer target changed before parent fsync")
            os.fsync(parent_fd)
            self._assert_dir_path_identity(path.parent, parent_fd)

    @contextmanager
    def _locked(
        self,
        path: Path,
        kind: str,
        binding: dict[str, str],
    ) -> Iterator[None]:
        exact = self._lock_bytes(kind, binding)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        with self._root_lock_scope():
            self._assert_active_locks()
            with self._parent_fd(path, create=True) as (parent_fd, name):
                self._assert_dir_path_identity(path.parent, parent_fd)
                try:
                    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
                except OSError as exc:
                    raise self._fail(f"cannot open {kind} lock") from exc
                try:
                    observed = os.fstat(fd)
                    self._require_owned_regular_stat(observed, f"{kind} lock")
                    self._assert_dir_path_identity(path.parent, parent_fd)
                    self._require_entry_identity(
                        parent_fd, name, observed, f"{kind} lock"
                    )
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    self._assert_dir_path_identity(path.parent, parent_fd)
                    self._require_entry_identity(
                        parent_fd, name, observed, f"{kind} lock"
                    )
                    current = self._read_fd(fd, max_bytes=_MAX_METADATA_BYTES)
                    if not current:
                        self._write_record_fd(fd, exact)
                        os.fsync(parent_fd)
                    elif current != exact:
                        raise self._fail(f"{kind} lock metadata drifted")
                    self._assert_dir_path_identity(path.parent, parent_fd)
                    active = self._active_lock_records()
                    active.append(
                        _ActiveLockRecord(
                            parent_path=path.parent,
                            parent_fd=parent_fd,
                            name=name,
                            observed=observed,
                            kind=kind,
                        )
                    )
                    try:
                        yield
                    finally:
                        try:
                            self._assert_dir_path_identity(path.parent, parent_fd)
                            self._require_entry_identity(
                                parent_fd, name, observed, f"{kind} lock"
                            )
                        finally:
                            active.pop()
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    @contextmanager
    def _ordered_locks(
        self,
        paths: list[Path],
        binding: dict[str, str],
    ) -> Iterator[None]:
        if len(paths) == 1:
            with self._locked(paths[0], "snapshot", binding):
                yield
            return
        with self._locked(paths[0], "spool", binding):
            with self._locked(paths[1], "staging", binding):
                yield

    def _lock_bytes(self, kind: str, binding: dict[str, str]) -> bytes:
        return self._canonical({"binding": binding, "kind": kind, "schema": _LOCK_SCHEMA})

    def _require_lock_binding(
        self, path: Path, kind: str, binding: dict[str, str]
    ) -> None:
        if self._read_private(path) != self._lock_bytes(kind, binding):
            raise self._fail(f"{kind} lock metadata drifted")

    @staticmethod
    def _resource_binding(intent: MaterializationIntentV4) -> dict[str, str]:
        return {
            "attempt_id": intent.attempt_id,
            "fence_identity": intent.fence_identity,
            "materialization_intent_sha256": intent.sha256,
        }

    def _path(self, relpath: str) -> Path:
        self._assert_root_stable()
        validate_relative_resource_path_v4(relpath, "v4 scratch resource")
        if any(
            len(os.fsencode(part)) > self._name_max
            for part in PurePosixPath(relpath).parts
        ):
            raise self._fail("scratch resource component exceeds NAME_MAX")
        candidate = self._root.joinpath(*PurePosixPath(relpath).parts)
        if self._root not in candidate.parents:
            raise self._fail("scratch resource escaped root")
        self._assert_existing_ancestors(candidate.parent)
        return candidate

    def _ensure_root(self) -> None:
        if self._root.exists() or self._root.is_symlink():
            if self._root.is_symlink() or not self._root.is_dir():
                raise ValueError("v4 scratch root is not a directory")
        else:
            self._root.mkdir(parents=True, mode=0o700)
        observed = self._root.stat(follow_symlinks=False)
        if (
            observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise ValueError("v4 scratch root is not private to the current user")

    def _assert_root_stable(self) -> None:
        observed = self._root.stat(follow_symlinks=False)
        if (
            observed.st_dev,
            observed.st_ino,
            observed.st_uid,
            observed.st_mode,
        ) != self._root_identity:
            raise self._fail("scratch root identity changed")

    def _assert_existing_ancestors(self, path: Path) -> None:
        current = self._root
        try:
            parts = path.relative_to(self._root).parts
        except ValueError as exc:
            raise self._fail("scratch path escaped root") from exc
        for part in parts:
            current /= part
            if not current.exists() and not current.is_symlink():
                break
            observed = current.stat(follow_symlinks=False)
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
                raise self._fail("scratch path has an unsafe ancestor")
            if (
                observed.st_uid != os.getuid()
                or observed.st_dev != self._root_identity[0]
                or stat.S_IMODE(observed.st_mode) != 0o700
            ):
                raise self._fail("scratch path ancestor is not private")

    def _relative_parts(self, path: Path) -> tuple[str, ...]:
        try:
            parts = path.relative_to(self._root).parts
        except ValueError as exc:
            raise self._fail("scratch path escaped root") from exc
        if any(
            not part
            or part in {".", ".."}
            or part != unicodedata.normalize("NFC", part)
            or len(os.fsencode(part)) > self._name_max
            for part in parts
        ):
            raise self._fail("scratch path has an unsafe component")
        return parts

    @contextmanager
    def _root_lock_scope(self) -> Iterator[None]:
        coordinator = self._root_lock_coordinator
        with coordinator.process_lock:
            depth = int(getattr(coordinator.local, "depth", 0))
            if depth == 0:
                root_fd = self._open_dir_fd(self._root)
                try:
                    fcntl.flock(root_fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(root_fd)
                    raise
                coordinator.local.root_fd = root_fd
            coordinator.local.depth = depth + 1
            try:
                yield
            finally:
                remaining = int(coordinator.local.depth) - 1
                coordinator.local.depth = remaining
                if remaining == 0:
                    root_fd = cast(int, coordinator.local.root_fd)
                    try:
                        self._assert_dir_path_identity(self._root, root_fd)
                    finally:
                        fcntl.flock(root_fd, fcntl.LOCK_UN)
                        os.close(root_fd)
                        del coordinator.local.root_fd

    def _active_lock_records(
        self,
    ) -> list[_ActiveLockRecord]:
        records = getattr(self._active_locks, "records", None)
        if records is None:
            records = []
            self._active_locks.records = records
        return cast(list[_ActiveLockRecord], records)

    def _assert_active_locks(self) -> None:
        for record in self._active_lock_records():
            self._assert_dir_path_identity(record.parent_path, record.parent_fd)
            self._require_entry_identity(
                record.parent_fd,
                record.name,
                record.observed,
                f"{record.kind} lock",
            )

    def _assert_dir_path_identity(self, path: Path, directory_fd: int) -> None:
        expected = self._identity(os.fstat(directory_fd))
        with self._open_dir(path) as current_fd:
            if self._identity(os.fstat(current_fd)) != expected:
                raise self._fail("scratch directory path was replaced")

    def _open_dir_fd(self, path: Path, *, create: bool = False) -> int:
        self._assert_root_stable()
        parts = self._relative_parts(path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            current_fd = os.open(self._root, flags)
        except OSError as exc:
            raise self._fail("cannot pin scratch root") from exc
        try:
            root_stat = os.fstat(current_fd)
            if (
                root_stat.st_dev,
                root_stat.st_ino,
                root_stat.st_uid,
                root_stat.st_mode,
            ) != self._root_identity:
                raise self._fail("scratch root changed while opening")
            for part in parts:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise self._fail("scratch directory is absent") from None
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                        os.fsync(current_fd)
                    except OSError as exc:
                        raise self._fail("cannot create safe scratch directory") from exc
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise self._fail("scratch directory is unsafe") from exc
                try:
                    observed = os.fstat(next_fd)
                    self._require_owned_dir_stat(observed, "scratch directory")
                    self._require_entry_identity(
                        current_fd, part, observed, "scratch directory"
                    )
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @contextmanager
    def _open_dir(self, path: Path, *, create: bool = False) -> Iterator[int]:
        fd = self._open_dir_fd(path, create=create)
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _parent_fd(
        self, path: Path, *, create: bool = False
    ) -> Iterator[tuple[int, str]]:
        parts = self._relative_parts(path)
        if not parts:
            raise self._fail("scratch root cannot be used as a resource file")
        parent_fd = self._open_dir_fd(path.parent, create=create)
        try:
            yield parent_fd, parts[-1]
        finally:
            os.close(parent_fd)

    @contextmanager
    def _pinned_tree(
        self,
        path: Path,
        *,
        max_files: int = 100_000,
        max_bytes: int = 32 * 1024 * 1024 * 1024,
        allow_empty_directories: bool = False,
    ) -> Iterator[PinnedArtifactTree]:
        with self._open_dir(path) as root_fd:
            with PinnedArtifactTree.from_root_fd(
                display_root=path,
                root_fd=root_fd,
                max_files=max_files,
                max_bytes=max_bytes,
                require_private_modes=True,
                allow_empty_directories=allow_empty_directories,
            ) as tree:
                yield tree

    def _mkdir_exact(self, path: Path) -> os.stat_result:
        with self._parent_fd(path, create=True) as (parent_fd, name):
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise self._fail("private directory already exists") from exc
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            self._require_owned_dir_stat(observed, "new private directory")
            os.fsync(parent_fd)
            return observed

    def _path_identity(self, path: Path) -> tuple[int, int]:
        with self._parent_fd(path) as (parent_fd, name):
            try:
                observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise self._fail("scratch resource is absent or unsafe") from exc
            if (
                stat.S_ISLNK(observed.st_mode)
                or observed.st_dev != self._root_identity[0]
                or not (stat.S_ISREG(observed.st_mode) or stat.S_ISDIR(observed.st_mode))
            ):
                raise self._fail("scratch resource is unsafe")
            return self._identity(observed)

    def _directory_is_empty(self, path: Path) -> bool:
        with self._open_dir(path) as directory_fd:
            return not os.listdir(directory_fd)

    def _try_path_stat(self, path: Path) -> os.stat_result | None:
        if path == self._root:
            with self._open_dir(self._root) as root_fd:
                return os.fstat(root_fd)
        try:
            with self._parent_fd(path) as (parent_fd, name):
                try:
                    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
        except ParserOutputContractError as exc:
            if str(exc).endswith("scratch directory is absent"):
                return None
            raise

    def _require_entry_identity(
        self,
        parent_fd: int,
        name: str,
        opened: os.stat_result,
        label: str,
    ) -> None:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise self._fail(f"{label} path disappeared") from exc
        if self._identity(current) != self._identity(opened):
            raise self._fail(f"{label} path changed while opening")

    @staticmethod
    def _identity(observed: os.stat_result) -> tuple[int, int]:
        return observed.st_dev, observed.st_ino

    def _require_owned_dir_stat(
        self, observed: os.stat_result, label: str
    ) -> None:
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.getuid()
            or observed.st_dev != self._root_identity[0]
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise self._fail(f"{label} is unsafe")

    def _remove_tree_contents(
        self,
        directory_fd: int,
        *,
        before_effect: Callable[[], None] | None,
        last_file_name: str | None = None,
    ) -> None:
        names = sorted(os.listdir(directory_fd))
        if last_file_name is not None and last_file_name in names:
            names.remove(last_file_name)
            names.append(last_file_name)
        for name in names:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            identity = self._identity(observed)
            if stat.S_ISDIR(observed.st_mode):
                self._require_owned_dir_stat(observed, "owned tree directory")
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    if self._identity(os.fstat(child_fd)) != identity:
                        raise self._fail("owned tree directory changed while opening")
                    self._remove_tree_contents(
                        child_fd,
                        before_effect=before_effect,
                        last_file_name=None,
                    )
                    if before_effect is not None:
                        before_effect()
                    current = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if self._identity(current) != identity or os.listdir(child_fd):
                        raise self._fail("owned tree directory changed before deletion")
                    os.rmdir(name, dir_fd=directory_fd)
                finally:
                    os.close(child_fd)
                continue
            self._require_owned_regular_stat(observed, "owned tree file")
            if before_effect is not None:
                before_effect()
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            self._require_owned_regular_stat(current, "owned tree file")
            if self._identity(current) != identity:
                raise self._fail("owned tree file was replaced before deletion")
            os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)

    def _measure_private_tree(
        self,
        directory_fd: int,
        *,
        max_files: int,
        max_bytes: int,
        totals: list[int],
    ) -> None:
        casefold_names: set[str] = set()
        for name in sorted(os.listdir(directory_fd)):
            if name != unicodedata.normalize("NFC", name):
                raise self._fail("private output path is not NFC")
            portable_name = name.casefold()
            if portable_name in casefold_names:
                raise self._fail("private output has a case-insensitive collision")
            casefold_names.add(portable_name)
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode):
                self._require_owned_dir_stat(observed, "private output directory")
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    if self._identity(os.fstat(child_fd)) != self._identity(observed):
                        raise self._fail("private output directory changed while opening")
                    self._measure_private_tree(
                        child_fd,
                        max_files=max_files,
                        max_bytes=max_bytes,
                        totals=totals,
                    )
                finally:
                    os.close(child_fd)
                continue
            self._require_owned_regular_stat(observed, "private output file")
            totals[0] += 1
            totals[1] += observed.st_size
            if totals[0] > max_files or totals[1] > max_bytes:
                raise self._fail("private output exceeded its allowance")

    def _fsync_tree_fd(self, directory_fd: int) -> None:
        for name in sorted(os.listdir(directory_fd)):
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode):
                self._require_owned_dir_stat(observed, "fsync tree directory")
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    if self._identity(os.fstat(child_fd)) != self._identity(observed):
                        raise self._fail("fsync tree directory changed while opening")
                    self._fsync_tree_fd(child_fd)
                finally:
                    os.close(child_fd)
                continue
            self._require_owned_regular_stat(observed, "fsync tree file")
            file_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            try:
                if self._identity(os.fstat(file_fd)) != self._identity(observed):
                    raise self._fail("fsync tree file changed while opening")
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        os.fsync(directory_fd)

    def _ensure_parent(self, path: Path) -> None:
        with self._open_dir(path.parent, create=True):
            pass

    def _ensure_dirs_beneath(
        self,
        root: Path,
        target: Path,
        *,
        journal_root: Path | None = None,
        journal: _StagingWriteJournal | None = None,
    ) -> None:
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise self._fail("ZIP directory escaped extraction root") from exc
        if (journal_root is None) != (journal is None):
            raise self._fail("ZIP directory journal is incomplete")
        if journal is None:
            with self._open_dir(target, create=True):
                pass
            return
        assert journal_root is not None
        current = root
        relative = target.relative_to(root)
        for part in relative.parts:
            current = current / part
            journal_relative = PurePosixPath(
                current.relative_to(journal_root).as_posix()
            )
            observed = self._try_path_stat(current)
            if observed is None:
                self._mkdir_exact(current)
                journal.add_directory(journal_relative)
                continue
            self._require_owned_dir_stat(observed, "ZIP extraction directory")
            if journal_relative not in journal.directories:
                raise self._fail("ZIP extraction directory was not created by this session")

    def _require_file(
        self, path: Path, sha256: str, byte_count: int
    ) -> tuple[int, int]:
        with self._parent_fd(path) as (parent_fd, name):
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise self._fail("exact local file is absent or unsafe") from exc
            try:
                observed = os.fstat(fd)
                self._require_owned_regular_stat(observed, "exact local file")
                self._require_entry_identity(
                    parent_fd, name, observed, "exact local file"
                )
                digest, count = self._hash_fd(fd)
                if digest != sha256 or count != byte_count:
                    raise self._fail("exact local file identity drifted")
                after = os.fstat(fd)
                self._require_entry_identity(
                    parent_fd, name, after, "exact local file"
                )
                if self._identity(after) != self._identity(observed):
                    raise self._fail("exact local file changed while hashing")
                return self._identity(observed)
            finally:
                os.close(fd)

    def _require_owned_regular_stat(
        self, observed: os.stat_result, label: str
    ) -> None:
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or observed.st_nlink != 1
            or observed.st_dev != self._root_identity[0]
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise ParserOutputContractError(f"MinerU v4 backend: {label} is unsafe")

    def _read_private(self, path: Path) -> bytes:
        with self._parent_fd(path) as (parent_fd, name):
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise self._fail("private metadata is absent or unsafe") from exc
            try:
                observed = os.fstat(fd)
                self._require_owned_regular_stat(observed, "private metadata")
                self._require_entry_identity(
                    parent_fd, name, observed, "private metadata"
                )
                exact = self._read_fd(fd)
                after = os.fstat(fd)
                self._require_entry_identity(
                    parent_fd, name, after, "private metadata"
                )
                if self._identity(after) != self._identity(observed):
                    raise self._fail("private metadata changed while reading")
                return exact
            finally:
                os.close(fd)

    def _write_private(self, path: Path, exact: bytes) -> os.stat_result:
        with self._parent_fd(path, create=True) as (parent_fd, name):
            fd = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                observed = os.fstat(fd)
                self._require_owned_regular_stat(observed, "new private metadata")
                self._require_entry_identity(
                    parent_fd, name, observed, "new private metadata"
                )
                self._write_all(fd, exact)
                os.fsync(fd)
                after = os.fstat(fd)
                self._require_owned_regular_stat(after, "new private metadata")
                self._require_entry_identity(
                    parent_fd,
                    name,
                    after,
                    "new private metadata",
                )
                if self._identity(after) != self._identity(observed):
                    raise self._fail("new private metadata changed while writing")
            finally:
                os.close(fd)
            os.fsync(parent_fd)
            return after

    def _remove_owned_file(
        self,
        path: Path,
        *,
        allow_absent: bool,
        before_effect: Callable[[], None] | None = None,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        with self._parent_fd(path) as (parent_fd, name):
            try:
                observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if allow_absent:
                    return
                raise self._fail("owned file disappeared") from None
            self._require_owned_regular_stat(observed, "owned file")
            identity = self._identity(observed)
            if expected_identity is not None and identity != expected_identity:
                raise self._fail("owned file identity changed before deletion")
            if before_effect is not None:
                before_effect()
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            self._require_owned_regular_stat(current, "owned file")
            if self._identity(current) != identity:
                raise self._fail("owned file was replaced before deletion")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)

    def _remove_owned_tree(
        self,
        path: Path,
        *,
        before_effect: Callable[[], None] | None = None,
        expected_identity: tuple[int, int] | None = None,
        last_file_name: str | None = None,
    ) -> None:
        with self._parent_fd(path) as (parent_fd, name):
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            self._require_owned_dir_stat(observed, "owned tree")
            identity = self._identity(observed)
            if expected_identity is not None and identity != expected_identity:
                raise self._fail("owned tree identity changed before deletion")
            root_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            try:
                if self._identity(os.fstat(root_fd)) != identity:
                    raise self._fail("owned tree changed while opening")
                self._remove_tree_contents(
                    root_fd,
                    before_effect=before_effect,
                    last_file_name=last_file_name,
                )
                if before_effect is not None:
                    before_effect()
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if self._identity(current) != identity or os.listdir(root_fd):
                    raise self._fail("owned tree changed before deletion")
                os.rmdir(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(root_fd)

    def _preflight_private_tree(
        self,
        root: Path,
        *,
        max_files: int,
        max_bytes: int,
    ) -> None:
        with self._open_dir(root) as root_fd:
            self._measure_private_tree(
                root_fd,
                max_files=max_files,
                max_bytes=max_bytes,
                totals=[0, 0],
            )

    def _exclusive_rename(
        self,
        source: Path,
        target: Path,
        *,
        expected_source_identity: tuple[int, int],
        before_rename: Callable[[], object] | None = None,
        after_rename: Callable[[], object] | None = None,
    ) -> None:
        """Atomically rename without replacing an existing destination."""
        with self._parent_fd(source) as (source_parent, source_name):
            with self._parent_fd(target, create=True) as (target_parent, target_name):
                source_parent_identity = self._identity(os.fstat(source_parent))
                target_parent_identity = self._identity(os.fstat(target_parent))
                self._assert_dir_path_identity(source.parent, source_parent)
                self._assert_dir_path_identity(target.parent, target_parent)
                source_stat = os.stat(
                    source_name, dir_fd=source_parent, follow_symlinks=False
                )
                if self._identity(source_stat) != expected_source_identity:
                    raise self._fail("exclusive rename source was replaced")
                try:
                    os.stat(target_name, dir_fd=target_parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise self._fail("exclusive rename destination exists")
                if before_rename is not None:
                    before_rename()
                self._assert_dir_path_identity(source.parent, source_parent)
                self._assert_dir_path_identity(target.parent, target_parent)
                current_source = os.stat(
                    source_name, dir_fd=source_parent, follow_symlinks=False
                )
                if self._identity(current_source) != expected_source_identity:
                    raise self._fail("exclusive rename source was replaced")
                libc = ctypes.CDLL(None, use_errno=True)
                source_bytes = os.fsencode(source_name)
                target_bytes = os.fsencode(target_name)
                if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
                    rename = libc.renameatx_np
                    rename.argtypes = (
                        ctypes.c_int,
                        ctypes.c_char_p,
                        ctypes.c_int,
                        ctypes.c_char_p,
                        ctypes.c_uint,
                    )
                    rename.restype = ctypes.c_int
                    result = rename(
                        source_parent,
                        ctypes.c_char_p(source_bytes),
                        target_parent,
                        ctypes.c_char_p(target_bytes),
                        ctypes.c_uint(0x00000004),  # RENAME_EXCL
                    )
                elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
                    rename = libc.renameat2
                    rename.argtypes = (
                        ctypes.c_int,
                        ctypes.c_char_p,
                        ctypes.c_int,
                        ctypes.c_char_p,
                        ctypes.c_uint,
                    )
                    rename.restype = ctypes.c_int
                    result = rename(
                        source_parent,
                        ctypes.c_char_p(source_bytes),
                        target_parent,
                        ctypes.c_char_p(target_bytes),
                        ctypes.c_uint(1),  # RENAME_NOREPLACE
                    )
                else:
                    raise self._fail("exclusive rename is unavailable")
                if result != 0:
                    error = ctypes.get_errno()
                    if error in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise self._fail("exclusive rename destination exists")
                    raise OSError(error, os.strerror(error), str(source), str(target))
                if after_rename is not None:
                    after_rename()
                try:
                    os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise self._fail("exclusive rename source remained present")
                published = os.stat(
                    target_name, dir_fd=target_parent, follow_symlinks=False
                )
                if self._identity(published) != expected_source_identity:
                    raise self._fail("exclusive rename target identity drifted")
                if (
                    self._identity(os.fstat(source_parent)) != source_parent_identity
                    or self._identity(os.fstat(target_parent)) != target_parent_identity
                ):
                    raise self._fail("exclusive rename parent identity drifted")
                self._assert_dir_path_identity(source.parent, source_parent)
                self._assert_dir_path_identity(target.parent, target_parent)
                os.fsync(source_parent)
                if target_parent_identity != source_parent_identity:
                    os.fsync(target_parent)
                self._assert_dir_path_identity(source.parent, source_parent)
                self._assert_dir_path_identity(target.parent, target_parent)

    @staticmethod
    def _hash_fd(fd: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, _CHUNK_BYTES):
            count += len(chunk)
            digest.update(chunk)
        return "sha256:" + digest.hexdigest(), count

    def _fsync_tree(self, root: Path) -> None:
        with self._open_dir(root) as root_fd:
            self._fsync_tree_fd(root_fd)

    def _fsync_dir(self, path: Path) -> None:
        with self._open_dir(path) as fd:
            os.fsync(fd)

    @staticmethod
    def _write_all(fd: int, value: bytes) -> None:
        view = memoryview(value)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short local write")
            view = view[written:]

    @staticmethod
    def _read_fd(fd: int, *, max_bytes: int = _MAX_METADATA_BYTES) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        parts: list[bytes] = []
        byte_count = 0
        while chunk := os.read(fd, 64 * 1024):
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise ParserOutputContractError(
                    "MinerU v4 backend: private metadata exceeds its byte limit"
                )
            parts.append(chunk)
        return b"".join(parts)

    @classmethod
    def _write_record_fd(cls, fd: int, exact: bytes) -> None:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        cls._write_all(fd, exact)
        os.fsync(fd)

    def _closed_response(self, exact: bytes, fields: set[str]) -> dict[str, Any]:
        try:
            value = strict_json_loads(exact)
        except ValueError as exc:
            raise self._fail("provider ACK response is not strict JSON") from exc
        if type(value) is not dict or set(value) != fields or self._canonical(value) != exact:
            raise self._fail("provider ACK response is not closed canonical JSON")
        return cast(dict[str, Any], value)

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def _marker_bytes(self, intent: MaterializationIntentV4) -> bytes:
        return self._canonical(
            {
                "attempt_id": intent.attempt_id,
                "fence_identity": intent.fence_identity,
                "materialization_intent_sha256": intent.sha256,
                "output_relpath": intent.output_relpath,
                "schema": _MARKER_SCHEMA,
                "staging_relpath": intent.staging_relpath,
            }
        )

    def _spool_owner_bytes(self, intent: MaterializationIntentV4) -> bytes:
        return self._canonical(
            {
                "artifact_byte_count": intent.artifact_byte_count,
                "artifact_sha256": intent.artifact_sha256,
                "attempt_id": intent.attempt_id,
                "fence_identity": intent.fence_identity,
                "part_relpath": intent.spool_part_relpath,
                "schema": _OWNER_SCHEMA,
                "spool_relpath": intent.spool_relpath,
            }
        )

    @staticmethod
    def _digest(exact: bytes) -> str:
        return "sha256:" + hashlib.sha256(exact).hexdigest()

    def _guard(
        self,
        guard: V4ClaimGuard,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
    ) -> None:
        guard.assert_current_under_resource_lock(checkpoint=checkpoint, claim=claim)
        self._assert_active_locks()

    def _observe_clock(self) -> None:
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise ValueError("v4 backend clock is invalid")

    @staticmethod
    def _fail(message: str) -> ParserOutputContractError:
        return ParserOutputContractError(f"MinerU v4 backend: {message}")
