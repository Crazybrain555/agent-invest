"""Read official MinerU Medium artifacts without legacy repair or reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import codecs
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from disclosure_anchor.application.contracts.provider_document import (
    PhysicalTableLogicalStatus,
    ProviderArtifact,
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
    provider_payload_field_contract,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_EXPECTED_VERSION = "3.4.4"
_EXPECTED_BACKEND = "hybrid"
_EXPECTED_EFFORT = "medium"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PARSED_JSON_ROLES = frozenset(
    {"content_list", "content_list_v2", "middle_json", "model_json"}
)

_REQUIRED_SUFFIXES = {
    "content_list_v2": "_content_list_v2.json",
    "middle_json": "_middle.json",
    "model_json": "_model.json",
}
_OPTIONAL_SUFFIXES = {
    "markdown": ".md",
    "layout_pdf": "_layout.pdf",
    "origin_pdf": "_origin.pdf",
}
_KNOWN_PAYLOAD_FIELDS = frozenset(
    {
        "chart_caption",
        "chart_footnote",
        "code_body",
        "code_caption",
        "code_footnote",
        "content",
        "image_caption",
        "image_footnote",
        "list_items",
        "table_body",
        "table_caption",
        "table_footnote",
        "table_html",
        "text",
    }
)
_IMAGE_PATH_FIELDS = ("img_path", "image_path", "image")
_COMPATIBLE_TYPED_ANNOTATIONS = frozenset(
    {
        ("header", "page_header"),
        ("text", "paragraph"),
        ("text", "title"),
    }
)

_MAX_TREE_DEPTH = 64
_MAX_TREE_FILES = 100_000
_MAX_TREE_BYTES = 32 * 1024 * 1024 * 1024
_MAX_JSON_BYTES = 4 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    link_count: int
    byte_count: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> _FileIdentity:
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


def _identity_without_changed_ns(
    value: _FileIdentity,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.device,
        value.inode,
        value.mode,
        value.uid,
        value.link_count,
        value.byte_count,
        value.modified_ns,
    )


def _stable_identity(value: _FileIdentity) -> tuple[int, int, int, int]:
    return value.device, value.inode, value.mode, value.uid


@dataclass(frozen=True, slots=True)
class PinnedArtifactFile:
    """One regular file read from a pinned root directory descriptor."""

    relative_path: PurePosixPath
    identity: _FileIdentity
    sha256: str
    size_bytes: int
    leading_bytes: bytes


@dataclass(frozen=True, slots=True)
class PinnedArtifactReadResult:
    document: ProviderDocument
    artifact_root_relpath: PurePosixPath


class PinnedArtifactTree:
    """Immutable-by-verification view over an already published output tree.

    The tree never resolves a child through the process cwd or a mutable
    absolute path.  Every component is opened relative to a pinned directory
    descriptor with ``O_NOFOLLOW``.  A second topology pass closes mutations
    which occur after an individual file was hashed.
    """

    def __init__(
        self,
        *,
        display_root: Path,
        root_fd: int,
        max_files: int = _MAX_TREE_FILES,
        max_bytes: int = _MAX_TREE_BYTES,
        require_private_modes: bool = False,
        allow_empty_directories: bool = False,
    ) -> None:
        _require_dirfd_flags()
        if type(max_files) is not int or max_files < 1:
            raise ParserOutputContractError("MinerU artifact file limit is invalid")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ParserOutputContractError("MinerU artifact byte limit is invalid")
        if type(require_private_modes) is not bool:
            raise ParserOutputContractError("MinerU artifact mode policy is invalid")
        if type(allow_empty_directories) is not bool:
            raise ParserOutputContractError(
                "MinerU artifact empty-directory policy is invalid"
            )
        self._display_root = display_root.absolute()
        self._root_fd = os.dup(root_fd)
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._require_private_modes = require_private_modes
        self._allow_empty_directories = allow_empty_directories
        self._closed = False
        try:
            observed = os.fstat(self._root_fd)
            self._root_identity = _directory_identity(
                observed,
                root_device=observed.st_dev,
                label="MinerU output root",
            )
            self._require_private_identity(
                self._root_identity,
                is_directory=True,
                label="MinerU output root",
            )
            self._assert_display_path()
            self._directories: dict[PurePosixPath, _FileIdentity] = {}
            self._directory_entries: dict[PurePosixPath, tuple[str, ...]] = {}
            self._files: dict[PurePosixPath, PinnedArtifactFile] = {}
            self._scan_initial()
        except BaseException:
            os.close(self._root_fd)
            self._closed = True
            raise

    @classmethod
    def open_path(
        cls,
        root: Path,
        *,
        max_files: int = _MAX_TREE_FILES,
        max_bytes: int = _MAX_TREE_BYTES,
        require_private_modes: bool = False,
        allow_empty_directories: bool = False,
    ) -> PinnedArtifactTree:
        _require_dirfd_flags()
        flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
        try:
            fd = os.open(root, flags)
        except OSError as exc:
            raise ParserOutputContractError(
                f"cannot open pinned MinerU output root: {root}"
            ) from exc
        try:
            return cls(
                display_root=root,
                root_fd=fd,
                max_files=max_files,
                max_bytes=max_bytes,
                require_private_modes=require_private_modes,
                allow_empty_directories=allow_empty_directories,
            )
        finally:
            os.close(fd)

    @classmethod
    def from_root_fd(
        cls,
        *,
        display_root: Path,
        root_fd: int,
        max_files: int = _MAX_TREE_FILES,
        max_bytes: int = _MAX_TREE_BYTES,
        require_private_modes: bool = False,
        allow_empty_directories: bool = False,
    ) -> PinnedArtifactTree:
        return cls(
            display_root=display_root,
            root_fd=root_fd,
            max_files=max_files,
            max_bytes=max_bytes,
            require_private_modes=require_private_modes,
            allow_empty_directories=allow_empty_directories,
        )

    def __enter__(self) -> PinnedArtifactTree:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            os.close(self._root_fd)
            self._closed = True

    @property
    def display_root(self) -> Path:
        return self._display_root

    @property
    def files(self) -> tuple[PinnedArtifactFile, ...]:
        self._require_open()
        return tuple(self._files[path] for path in sorted(self._files))

    @property
    def directory_paths(self) -> tuple[PurePosixPath, ...]:
        """Return the complete admitted directory topology, including ``.``."""

        self._require_open()
        return tuple(sorted(self._directories))

    @property
    def root_identity(self) -> tuple[int, int]:
        self._require_open()
        return self._root_identity.device, self._root_identity.inode

    def has_file(self, relative_path: PurePosixPath) -> bool:
        self._require_open()
        return _safe_relative(relative_path, label="MinerU artifact") in self._files

    def require_file(self, relative_path: PurePosixPath) -> PinnedArtifactFile:
        self._require_open()
        relative = _safe_relative(relative_path, label="MinerU artifact")
        try:
            return self._files[relative]
        except KeyError as exc:
            raise ParserOutputContractError(
                f"MinerU artifact is absent: {relative.as_posix()}"
            ) from exc

    def read_bytes(
        self,
        relative_path: PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read one already-pinned regular file without reopening by Path."""

        if type(max_bytes) is not int or max_bytes < 1:
            raise ParserOutputContractError("MinerU pinned byte limit is invalid")
        receipt = self.require_file(relative_path)
        if receipt.size_bytes > max_bytes:
            raise ParserOutputContractError("MinerU pinned file exceeds its limit")
        fd = self._open_regular(receipt.relative_path)
        try:
            before = _FileIdentity.from_stat(os.fstat(fd))
            if before != receipt.identity:
                raise ParserOutputContractError(
                    "MinerU pinned file identity changed before reading"
                )
            digest = hashlib.sha256()
            byte_count = 0
            chunks: list[bytes] = []
            while chunk := os.read(fd, min(1024 * 1024, max_bytes + 1)):
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise ParserOutputContractError(
                        "MinerU pinned file exceeded its limit while reading"
                    )
                digest.update(chunk)
                chunks.append(chunk)
            if (
                _FileIdentity.from_stat(os.fstat(fd)) != before
                or byte_count != receipt.size_bytes
                or "sha256:" + digest.hexdigest() != receipt.sha256
            ):
                raise ParserOutputContractError(
                    "MinerU pinned file changed while reading"
                )
            return b"".join(chunks)
        finally:
            os.close(fd)

    def load_json(
        self,
        relative_path: PurePosixPath,
        *,
        label: str,
        max_bytes: int = _MAX_JSON_BYTES,
    ) -> object:
        receipt = self.require_file(relative_path)
        if receipt.size_bytes > max_bytes:
            raise ParserOutputContractError(f"MinerU {label} JSON exceeds its limit")
        fd = self._open_regular(receipt.relative_path)
        try:
            before = _FileIdentity.from_stat(os.fstat(fd))
            if before != receipt.identity:
                raise ParserOutputContractError(
                    f"MinerU {label} JSON identity changed before parsing"
                )
            digest = _stream_digest(fd)[0]
            if digest != receipt.sha256:
                raise ParserOutputContractError(
                    f"MinerU {label} JSON changed before parsing"
                )
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                with os.fdopen(
                    os.dup(fd), "r", encoding="utf-8", errors="strict"
                ) as stream:
                    value = json.load(stream, parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ParserOutputContractError(f"invalid MinerU {label} JSON") from exc
            if _FileIdentity.from_stat(os.fstat(fd)) != before:
                raise ParserOutputContractError(
                    f"MinerU {label} JSON changed during parsing"
                )
            return value
        finally:
            os.close(fd)

    def validate_utf8(self, relative_path: PurePosixPath, *, label: str) -> None:
        receipt = self.require_file(relative_path)
        fd = self._open_regular(receipt.relative_path)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            before = _FileIdentity.from_stat(os.fstat(fd))
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                decoder.decode(chunk, final=False)
                digest.update(chunk)
            decoder.decode(b"", final=True)
            if (
                before != receipt.identity
                or _FileIdentity.from_stat(os.fstat(fd)) != before
                or "sha256:" + digest.hexdigest() != receipt.sha256
            ):
                raise ParserOutputContractError(
                    f"MinerU artifact role={label} changed during UTF-8 validation"
                )
        except UnicodeDecodeError as exc:
            raise ParserOutputContractError(
                f"cannot read MinerU artifact role={label}"
            ) from exc
        finally:
            os.close(fd)

    def verify_unchanged(self) -> None:
        self._require_open()
        self._assert_display_path()

    def verify_pinned_topology_unchanged(
        self, *, allow_root_rename_ctime: bool = False
    ) -> None:
        """Re-scan the exact pinned tree without resolving its display path.

        This is intentionally separate from :meth:`verify_unchanged`: an
        adapter may keep the root descriptor open across an atomic directory
        rename, after which the original display path no longer names the
        pinned inode.  Child topology and identities must still match the
        admission snapshot before the adapter may act on the renamed tree.
        """

        self._require_open()
        if type(allow_root_rename_ctime) is not bool:
            raise ParserOutputContractError(
                "MinerU pinned topology verification mode is invalid"
            )
        observed_directories: dict[PurePosixPath, _FileIdentity] = {}
        observed_entries: dict[PurePosixPath, tuple[str, ...]] = {}
        observed_files: dict[PurePosixPath, _FileIdentity] = {}
        self._scan_metadata(
            directory_fd=os.dup(self._root_fd),
            relative=PurePosixPath("."),
            depth=0,
            directories=observed_directories,
            entries=observed_entries,
            files=observed_files,
            allow_empty_directories=True,
        )
        expected_files = {
            path: receipt.identity for path, receipt in self._files.items()
        }
        expected_directories = self._directories
        if allow_root_rename_ctime:
            root = PurePosixPath(".")
            observed_root = observed_directories.get(root)
            expected_root = expected_directories.get(root)
            if (
                observed_root is None
                or expected_root is None
                or _identity_without_changed_ns(observed_root)
                != _identity_without_changed_ns(expected_root)
            ):
                raise ParserOutputContractError(
                    "MinerU artifact root changed across atomic rename"
                )
            expected_directories = {**expected_directories, root: observed_root}
        if (
            observed_directories != expected_directories
            or observed_entries != self._directory_entries
            or observed_files != expected_files
        ):
            raise ParserOutputContractError(
                "MinerU artifact tree changed during pinned admission"
            )

    def remove_exact_admitted_contents(
        self,
        *,
        before_effect: Callable[[], object],
        last_files: tuple[PurePosixPath, ...],
    ) -> None:
        """Delete only this admitted snapshot, with selected proof files last.

        The pinned root stays open throughout.  One complete verification
        admits the initial mutable suffix; each effect then resolves only an
        expected entry through pinned parent descriptors and checks its exact
        identity.  Complete scans are repeated at the ordinary/proof boundary,
        immediately after every proof-file guard, and at the empty-root end.
        Thus deletion is linear in admitted entries (times path depth), while
        unknown entries are never recursively removed and proof files remain
        last.
        """

        self._require_open()
        if not callable(before_effect) or type(last_files) is not tuple:
            raise ParserOutputContractError(
                "MinerU exact cleanup arguments are invalid"
            )
        ordered_last = tuple(
            _safe_relative(path, label="MinerU exact cleanup proof")
            for path in last_files
        )
        if len(set(ordered_last)) != len(ordered_last) or any(
            path not in self._files for path in ordered_last
        ):
            raise ParserOutputContractError(
                "MinerU exact cleanup proof files are absent or duplicated"
            )
        expected_directories = dict(self._directories)
        expected_entries = {
            path: set(names) for path, names in self._directory_entries.items()
        }
        expected_files = {
            path: receipt.identity for path, receipt in self._files.items()
        }

        def verify_remaining() -> None:
            self._verify_expected_mutable_topology(
                expected_directories=expected_directories,
                expected_entries=expected_entries,
                expected_files=expected_files,
            )

        def remove_file(
            relative: PurePosixPath,
            *,
            verify_after_guard: bool,
        ) -> None:
            before_effect()
            if verify_after_guard:
                verify_remaining()
            parent_fd = _open_parent_directory(
                self._root_fd,
                relative.parts[:-1],
                root_device=self._root_identity.device,
            )
            try:
                parent = relative.parent
                parent_identity = _directory_identity(
                    os.fstat(parent_fd),
                    root_device=self._root_identity.device,
                    label=f"MinerU exact cleanup parent {parent.as_posix()}",
                )
                if _stable_identity(parent_identity) != _stable_identity(
                    expected_directories[parent]
                ):
                    raise ParserOutputContractError(
                        "MinerU exact cleanup parent identity drifted"
                    )
                observed = os.stat(
                    relative.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if _FileIdentity.from_stat(observed) != expected_files[relative]:
                    raise ParserOutputContractError(
                        "MinerU exact cleanup file identity drifted"
                    )
                os.unlink(relative.name, dir_fd=parent_fd)
                try:
                    os.stat(
                        relative.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise ParserOutputContractError(
                        "MinerU exact cleanup file remained present"
                    )
                if _stable_identity(
                    _directory_identity(
                        os.fstat(parent_fd),
                        root_device=self._root_identity.device,
                        label=f"MinerU exact cleanup parent {parent.as_posix()}",
                    )
                ) != _stable_identity(expected_directories[parent]):
                    raise ParserOutputContractError(
                        "MinerU exact cleanup parent identity drifted"
                    )
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            del expected_files[relative]
            expected_entries[parent].remove(relative.name)

        last_set = set(ordered_last)
        verify_remaining()
        # Initial admission order is deterministic because every directory scan
        # consumes sorted names.  Reusing it avoids an additional N log N sort.
        for relative in tuple(path for path in expected_files if path not in last_set):
            remove_file(relative, verify_after_guard=False)
        verify_remaining()

        directories_by_depth: dict[int, list[PurePosixPath]] = {}
        for relative in expected_directories:
            if relative == PurePosixPath("."):
                continue
            directories_by_depth.setdefault(len(relative.parts), []).append(relative)
        for depth in sorted(directories_by_depth, reverse=True):
            for relative in reversed(directories_by_depth[depth]):
                before_effect()
                parent_fd = _open_parent_directory(
                    self._root_fd,
                    relative.parts[:-1],
                    root_device=self._root_identity.device,
                )
                try:
                    parent = relative.parent
                    parent_identity = _directory_identity(
                        os.fstat(parent_fd),
                        root_device=self._root_identity.device,
                        label=f"MinerU exact cleanup parent {parent.as_posix()}",
                    )
                    if _stable_identity(parent_identity) != _stable_identity(
                        expected_directories[parent]
                    ):
                        raise ParserOutputContractError(
                            "MinerU exact cleanup parent identity drifted"
                        )
                    observed = os.stat(
                        relative.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if _stable_identity(
                        _FileIdentity.from_stat(observed)
                    ) != _stable_identity(expected_directories[relative]):
                        raise ParserOutputContractError(
                            "MinerU exact cleanup directory identity drifted"
                        )
                    try:
                        os.rmdir(relative.name, dir_fd=parent_fd)
                    except OSError as exc:
                        raise ParserOutputContractError(
                            "MinerU exact cleanup directory is not exact and empty"
                        ) from exc
                    try:
                        os.stat(
                            relative.name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise ParserOutputContractError(
                            "MinerU exact cleanup directory remained present"
                        )
                    if _stable_identity(
                        _directory_identity(
                            os.fstat(parent_fd),
                            root_device=self._root_identity.device,
                            label=f"MinerU exact cleanup parent {parent.as_posix()}",
                        )
                    ) != _stable_identity(expected_directories[parent]):
                        raise ParserOutputContractError(
                            "MinerU exact cleanup parent identity drifted"
                        )
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
                del expected_directories[relative]
                del expected_entries[relative]
                expected_entries[parent].remove(relative.name)

        for relative in ordered_last:
            remove_file(relative, verify_after_guard=True)
        verify_remaining()
        if (
            expected_files
            or set(expected_directories) != {PurePosixPath(".")}
            or expected_entries != {PurePosixPath("."): set()}
        ):
            raise ParserOutputContractError(
                "MinerU exact cleanup did not empty its admitted root"
            )

    def _verify_expected_mutable_topology(
        self,
        *,
        expected_directories: dict[PurePosixPath, _FileIdentity],
        expected_entries: dict[PurePosixPath, set[str]],
        expected_files: dict[PurePosixPath, _FileIdentity],
    ) -> None:
        observed_directories: dict[PurePosixPath, _FileIdentity] = {}
        observed_entries: dict[PurePosixPath, tuple[str, ...]] = {}
        observed_files: dict[PurePosixPath, _FileIdentity] = {}
        self._scan_metadata(
            directory_fd=os.dup(self._root_fd),
            relative=PurePosixPath("."),
            depth=0,
            directories=observed_directories,
            entries=observed_entries,
            files=observed_files,
            allow_empty_directories=True,
        )
        if (
            {path: _stable_identity(value) for path, value in observed_directories.items()}
            != {
                path: _stable_identity(value)
                for path, value in expected_directories.items()
            }
            or {path: set(names) for path, names in observed_entries.items()}
            != expected_entries
            or observed_files != expected_files
        ):
            raise ParserOutputContractError(
                "MinerU exact cleanup topology changed"
            )

    def fsync_exact(self) -> None:
        """Durably flush the exact file/directory identities in this snapshot."""

        self._require_open()
        for relative, receipt in sorted(self._files.items()):
            fd = self._open_regular(relative)
            try:
                before = _FileIdentity.from_stat(os.fstat(fd))
                if before != receipt.identity:
                    raise ParserOutputContractError(
                        "MinerU pinned file changed before fsync"
                    )
                os.fsync(fd)
                if _FileIdentity.from_stat(os.fstat(fd)) != before:
                    raise ParserOutputContractError(
                        "MinerU pinned file changed during fsync"
                    )
            finally:
                os.close(fd)
        for relative in sorted(
            self._directories,
            key=lambda value: len(_relative_parts(value)),
            reverse=True,
        ):
            fd = _open_parent_directory(
                self._root_fd,
                _relative_parts(relative),
                root_device=self._root_identity.device,
            )
            try:
                before = _FileIdentity.from_stat(os.fstat(fd))
                if before != self._directories[relative]:
                    raise ParserOutputContractError(
                        "MinerU pinned directory changed before fsync"
                    )
                os.fsync(fd)
                if _FileIdentity.from_stat(os.fstat(fd)) != before:
                    raise ParserOutputContractError(
                        "MinerU pinned directory changed during fsync"
                    )
            finally:
                os.close(fd)
        self.verify_unchanged()
        self.verify_pinned_topology_unchanged()
        self._assert_display_path()

    def _scan_initial(self) -> None:
        file_count = [0]
        total_bytes = [0]
        self._scan_hashing(
            directory_fd=os.dup(self._root_fd),
            relative=PurePosixPath("."),
            depth=0,
            file_count=file_count,
            total_bytes=total_bytes,
        )

    def _scan_hashing(
        self,
        *,
        directory_fd: int,
        relative: PurePosixPath,
        depth: int,
        file_count: list[int],
        total_bytes: list[int],
    ) -> None:
        try:
            if depth > _MAX_TREE_DEPTH:
                raise ParserOutputContractError("MinerU artifact tree is too deep")
            before = _directory_identity(
                os.fstat(directory_fd),
                root_device=self._root_identity.device,
                label=f"MinerU artifact directory {relative.as_posix()}",
            )
            self._require_private_identity(
                before,
                is_directory=True,
                label=f"MinerU artifact directory {relative.as_posix()}",
            )
            names = _directory_names(directory_fd, relative=relative)
            if (
                relative != PurePosixPath(".")
                and not names
                and not self._allow_empty_directories
            ):
                raise ParserOutputContractError(
                    f"MinerU artifact tree contains an empty directory: {relative.as_posix()}"
                )
            self._directories[relative] = before
            self._directory_entries[relative] = names
            for name in names:
                child = _child_relative(relative, name)
                observed = _lstat_at(directory_fd, name, relative=child)
                if stat.S_ISDIR(observed.st_mode):
                    child_fd = _open_directory_at(
                        directory_fd,
                        name,
                        expected=observed,
                        root_device=self._root_identity.device,
                        relative=child,
                    )
                    self._scan_hashing(
                        directory_fd=child_fd,
                        relative=child,
                        depth=depth + 1,
                        file_count=file_count,
                        total_bytes=total_bytes,
                    )
                    continue
                expected_file = _regular_identity(
                    observed,
                    root_device=self._root_identity.device,
                    relative=child,
                )
                self._require_private_identity(
                    expected_file,
                    is_directory=False,
                    label=f"MinerU artifact {child.as_posix()}",
                )
                if (
                    file_count[0] + 1 > self._max_files
                    or total_bytes[0] + expected_file.byte_count > self._max_bytes
                ):
                    raise ParserOutputContractError(
                        "MinerU artifact tree exceeds its closed envelope"
                    )
                fd = _open_regular_at(
                    directory_fd,
                    name,
                    expected=observed,
                    root_device=self._root_identity.device,
                    relative=child,
                )
                try:
                    identity = _FileIdentity.from_stat(os.fstat(fd))
                    sha256, size_bytes, leading = _stream_digest(fd)
                    if _FileIdentity.from_stat(os.fstat(fd)) != identity:
                        raise ParserOutputContractError(
                            f"MinerU artifact changed while hashing: {child.as_posix()}"
                        )
                finally:
                    os.close(fd)
                file_count[0] += 1
                total_bytes[0] += size_bytes
                self._files[child] = PinnedArtifactFile(
                    relative_path=child,
                    identity=identity,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    leading_bytes=leading,
                )
            if (
                _directory_names(directory_fd, relative=relative) != names
                or _directory_identity(
                    os.fstat(directory_fd),
                    root_device=self._root_identity.device,
                    label=f"MinerU artifact directory {relative.as_posix()}",
                )
                != before
            ):
                raise ParserOutputContractError(
                    f"MinerU artifact directory changed during scan: {relative.as_posix()}"
                )
        finally:
            os.close(directory_fd)

    def _scan_metadata(
        self,
        *,
        directory_fd: int,
        relative: PurePosixPath,
        depth: int,
        directories: dict[PurePosixPath, _FileIdentity],
        entries: dict[PurePosixPath, tuple[str, ...]],
        files: dict[PurePosixPath, _FileIdentity],
        allow_empty_directories: bool = False,
    ) -> None:
        try:
            if depth > _MAX_TREE_DEPTH:
                raise ParserOutputContractError("MinerU artifact tree is too deep")
            before = _directory_identity(
                os.fstat(directory_fd),
                root_device=self._root_identity.device,
                label=f"MinerU artifact directory {relative.as_posix()}",
            )
            self._require_private_identity(
                before,
                is_directory=True,
                label=f"MinerU artifact directory {relative.as_posix()}",
            )
            names = _directory_names(directory_fd, relative=relative)
            if (
                relative != PurePosixPath(".")
                and not names
                and not allow_empty_directories
            ):
                raise ParserOutputContractError(
                    f"MinerU artifact tree contains an empty directory: {relative.as_posix()}"
                )
            directories[relative] = before
            entries[relative] = names
            for name in names:
                child = _child_relative(relative, name)
                observed = _lstat_at(directory_fd, name, relative=child)
                if stat.S_ISDIR(observed.st_mode):
                    child_fd = _open_directory_at(
                        directory_fd,
                        name,
                        expected=observed,
                        root_device=self._root_identity.device,
                        relative=child,
                    )
                    self._scan_metadata(
                        directory_fd=child_fd,
                        relative=child,
                        depth=depth + 1,
                        directories=directories,
                        entries=entries,
                        files=files,
                        allow_empty_directories=allow_empty_directories,
                    )
                    continue
                fd = _open_regular_at(
                    directory_fd,
                    name,
                    expected=observed,
                    root_device=self._root_identity.device,
                    relative=child,
                )
                try:
                    identity = _FileIdentity.from_stat(os.fstat(fd))
                    self._require_private_identity(
                        identity,
                        is_directory=False,
                        label=f"MinerU artifact {child.as_posix()}",
                    )
                    files[child] = identity
                finally:
                    os.close(fd)
            if (
                _directory_names(directory_fd, relative=relative) != names
                or _directory_identity(
                    os.fstat(directory_fd),
                    root_device=self._root_identity.device,
                    label=f"MinerU artifact directory {relative.as_posix()}",
                )
                != before
            ):
                raise ParserOutputContractError(
                    f"MinerU artifact directory changed during verification: {relative.as_posix()}"
                )
        finally:
            os.close(directory_fd)

    def _open_regular(self, relative_path: PurePosixPath) -> int:
        relative = _safe_relative(relative_path, label="MinerU artifact")
        parent_fd = _open_parent_directory(
            self._root_fd,
            relative.parts[:-1],
            root_device=self._root_identity.device,
        )
        try:
            expected = os.stat(
                relative.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            return _open_regular_at(
                parent_fd,
                relative.name,
                expected=expected,
                root_device=self._root_identity.device,
                relative=relative,
            )
        except OSError as exc:
            raise ParserOutputContractError(
                f"cannot reopen MinerU artifact: {relative.as_posix()}"
            ) from exc
        finally:
            os.close(parent_fd)

    def _assert_display_path(self) -> None:
        try:
            current = os.stat(self._display_root, follow_symlinks=False)
        except OSError as exc:
            raise ParserOutputContractError(
                "pinned MinerU output root path disappeared"
            ) from exc
        if _FileIdentity.from_stat(current) != self._root_identity:
            raise ParserOutputContractError("pinned MinerU output root path changed")

    def _require_private_identity(
        self,
        identity: _FileIdentity,
        *,
        is_directory: bool,
        label: str,
    ) -> None:
        if not self._require_private_modes:
            return
        expected_mode = 0o700 if is_directory else 0o600
        if stat.S_IMODE(identity.mode) != expected_mode:
            raise ParserOutputContractError(f"{label} has an unsafe private mode")

    def _require_open(self) -> None:
        if self._closed:
            raise ParserOutputContractError("pinned MinerU artifact tree is closed")


def _require_dirfd_flags() -> None:
    missing = tuple(
        name
        for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")
        if not hasattr(os, name)
    )
    if missing:
        raise ParserOutputContractError(
            "pinned MinerU artifact admission is unsupported: missing "
            + ", ".join(missing)
        )


def _safe_relative(path: PurePosixPath, *, label: str) -> PurePosixPath:
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ParserOutputContractError(f"{label} path is unsafe: {path.as_posix()}")
    return path


def _child_relative(parent: PurePosixPath, name: str) -> PurePosixPath:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ParserOutputContractError("MinerU artifact tree contains an unsafe name")
    if parent == PurePosixPath("."):
        return PurePosixPath(name)
    return parent / name


def _directory_identity(
    observed: os.stat_result,
    *,
    root_device: int,
    label: str,
) -> _FileIdentity:
    identity = _FileIdentity.from_stat(observed)
    if not stat.S_ISDIR(identity.mode):
        raise ParserOutputContractError(f"{label} is not a directory")
    if identity.device != root_device:
        raise ParserOutputContractError(f"{label} crosses a filesystem boundary")
    if identity.uid != os.getuid():
        raise ParserOutputContractError(f"{label} is not owned by the current user")
    if identity.link_count < 1:
        raise ParserOutputContractError(f"{label} has an invalid link count")
    return identity


def _regular_identity(
    observed: os.stat_result,
    *,
    root_device: int,
    relative: PurePosixPath,
) -> _FileIdentity:
    identity = _FileIdentity.from_stat(observed)
    if not stat.S_ISREG(identity.mode):
        raise ParserOutputContractError(
            f"MinerU artifact is not a regular file: {relative.as_posix()}"
        )
    if identity.device != root_device:
        raise ParserOutputContractError(
            f"MinerU artifact crosses a filesystem boundary: {relative.as_posix()}"
        )
    if identity.uid != os.getuid():
        raise ParserOutputContractError(
            f"MinerU artifact is not owned by the current user: {relative.as_posix()}"
        )
    if identity.link_count != 1:
        raise ParserOutputContractError(
            f"MinerU artifact must have exactly one link: {relative.as_posix()}"
        )
    return identity


def _directory_names(
    directory_fd: int,
    *,
    relative: PurePosixPath,
) -> tuple[str, ...]:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot list MinerU artifact directory: {relative.as_posix()}"
        ) from exc
    for name in names:
        if not isinstance(name, str) or not name or name in {".", ".."}:
            raise ParserOutputContractError(
                f"MinerU artifact directory contains an unsafe name: {relative.as_posix()}"
            )
        if "/" in name or "\x00" in name:
            raise ParserOutputContractError(
                f"MinerU artifact directory contains an unsafe name: {relative.as_posix()}"
            )
    return tuple(sorted(names))


def _lstat_at(
    directory_fd: int,
    name: str,
    *,
    relative: PurePosixPath,
) -> os.stat_result:
    try:
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot inspect MinerU artifact: {relative.as_posix()}"
        ) from exc
    if stat.S_ISLNK(observed.st_mode):
        raise ParserOutputContractError(
            f"MinerU artifact tree contains a symlink: {relative.as_posix()}"
        )
    if not stat.S_ISDIR(observed.st_mode) and not stat.S_ISREG(observed.st_mode):
        raise ParserOutputContractError(
            f"MinerU artifact tree contains a non-regular entry: {relative.as_posix()}"
        )
    return observed


def _open_directory_at(
    directory_fd: int,
    name: str,
    *,
    expected: os.stat_result,
    root_device: int,
    relative: PurePosixPath,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot pin MinerU artifact directory: {relative.as_posix()}"
        ) from exc
    try:
        expected_identity = _directory_identity(
            expected,
            root_device=root_device,
            label=f"MinerU artifact directory {relative.as_posix()}",
        )
        observed_identity = _directory_identity(
            os.fstat(fd),
            root_device=root_device,
            label=f"MinerU artifact directory {relative.as_posix()}",
        )
        if observed_identity != expected_identity:
            raise ParserOutputContractError(
                f"MinerU artifact directory changed while opening: {relative.as_posix()}"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular_at(
    directory_fd: int,
    name: str,
    *,
    expected: os.stat_result,
    root_device: int,
    relative: PurePosixPath,
) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot pin MinerU artifact: {relative.as_posix()}"
        ) from exc
    try:
        expected_identity = _regular_identity(
            expected,
            root_device=root_device,
            relative=relative,
        )
        observed_identity = _regular_identity(
            os.fstat(fd),
            root_device=root_device,
            relative=relative,
        )
        if observed_identity != expected_identity:
            raise ParserOutputContractError(
                f"MinerU artifact changed while opening: {relative.as_posix()}"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_parent_directory(
    root_fd: int,
    components: Sequence[str],
    *,
    root_device: int,
) -> int:
    current_fd = os.dup(root_fd)
    current = PurePosixPath(".")
    try:
        for component in components:
            relative = _child_relative(current, component)
            expected = _lstat_at(current_fd, component, relative=relative)
            if not stat.S_ISDIR(expected.st_mode):
                raise ParserOutputContractError(
                    f"MinerU artifact parent is not a directory: {relative.as_posix()}"
                )
            next_fd = _open_directory_at(
                current_fd,
                component,
                expected=expected,
                root_device=root_device,
                relative=relative,
            )
            os.close(current_fd)
            current_fd = next_fd
            current = relative
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _stream_digest(fd: int) -> tuple[str, int, bytes]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size_bytes = 0
        leading_bytes = b""
        while chunk := os.read(fd, 1024 * 1024):
            if not leading_bytes:
                leading_bytes = chunk[:16]
            digest.update(chunk)
            size_bytes += len(chunk)
        return "sha256:" + digest.hexdigest(), size_bytes, leading_bytes
    except OSError as exc:
        raise ParserOutputContractError("cannot stream MinerU artifact") from exc


def _relative_parts(relative: PurePosixPath) -> tuple[str, ...]:
    return () if relative == PurePosixPath(".") else relative.parts


def _relative_to_or_none(
    path: PurePosixPath,
    root: PurePosixPath,
) -> PurePosixPath | None:
    try:
        return _relative_to(path, root)
    except ParserOutputContractError:
        return None


def _relative_to(path: PurePosixPath, root: PurePosixPath) -> PurePosixPath:
    if root == PurePosixPath("."):
        return path
    try:
        return path.relative_to(root)
    except ValueError as exc:
        raise ParserOutputContractError(
            f"MinerU artifact {path.as_posix()} is outside {root.as_posix()}"
        ) from exc


class MinerUMediumArtifactReader:
    """Project the unique content-list directory subtree into diagnostic records."""

    def locate_artifact_root(self, output_dir: Path) -> Path:
        """Return the unique official artifact leaf without reading semantics."""

        with PinnedArtifactTree.open_path(output_dir) as tree:
            artifact_root = _locate_content_list_pinned(tree).relative_path.parent
            tree.verify_unchanged()
            return tree.display_root.joinpath(*_relative_parts(artifact_root))

    def read(self, output_dir: Path, *, source_pdf_sha256: str) -> ProviderDocument:
        return self.read_with_location(
            output_dir, source_pdf_sha256=source_pdf_sha256
        ).document

    def read_with_location(
        self, output_dir: Path, *, source_pdf_sha256: str
    ) -> PinnedArtifactReadResult:
        with PinnedArtifactTree.open_path(output_dir) as tree:
            return self.read_pinned(tree, source_pdf_sha256=source_pdf_sha256)

    def read_pinned(
        self, tree: PinnedArtifactTree, *, source_pdf_sha256: str
    ) -> PinnedArtifactReadResult:
        if not _SHA256_RE.fullmatch(source_pdf_sha256):
            raise ParserOutputContractError("source PDF sha256 must be canonical")
        content_file = _locate_content_list_pinned(tree)
        artifact_root = content_file.relative_path.parent
        stem = content_file.relative_path.name.removesuffix("_content_list.json")
        expected_stem = source_pdf_sha256.replace("sha256:", "sha256_", 1)
        if stem != expected_stem:
            raise ParserOutputContractError(
                "MinerU content-list stem does not match the source PDF sha256"
            )

        role_paths: dict[str, PurePosixPath] = {
            "content_list": content_file.relative_path
        }
        for role, suffix in _REQUIRED_SUFFIXES.items():
            role_path = artifact_root / f"{stem}{suffix}"
            role_paths[role] = tree.require_file(role_path).relative_path
        for role, suffix in _OPTIONAL_SUFFIXES.items():
            role_path = artifact_root / f"{stem}{suffix}"
            if tree.has_file(role_path):
                role_paths[role] = role_path

        tree_files = tuple(
            file
            for file in tree.files
            if _relative_to_or_none(file.relative_path, artifact_root) is not None
        )
        relative_roles = _artifact_roles_pinned(
            artifact_root=artifact_root,
            files=tree_files,
            explicit_role_paths=role_paths,
        )
        artifacts_tuple = tuple(
            _artifact_record_pinned(
                role=relative_roles[
                    _relative_to(file.relative_path, artifact_root).as_posix()
                ],
                file=file,
                tree=tree,
                artifact_root=artifact_root,
            )
            for file in tree_files
        )
        artifacts_by_relative = {
            artifact.relative_path: artifact for artifact in artifacts_tuple
        }
        content_items = _object_list(
            tree.load_json(role_paths["content_list"], label="content_list"),
            label="content_list",
        )
        typed_pages = _page_object_lists(
            tree.load_json(role_paths["content_list_v2"], label="content_list_v2"),
            label="content_list_v2",
        )
        page_sizes, parser_identity, ocr_enabled, middle_pages = _middle_document(
            tree.load_json(role_paths["middle_json"], label="middle_json"),
        )
        tree.load_json(role_paths["model_json"], label="model_json")

        block_specs: list[tuple[int, dict[str, Any], int, int]] = []
        page_orders = [0 for _ in page_sizes]
        content_indices_by_page: list[list[int]] = [[] for _ in page_sizes]
        for source_index, item in enumerate(content_items):
            page_index = _page_index(item, page_count=len(page_sizes))
            order_in_page = page_orders[page_index]
            page_orders[page_index] += 1
            content_indices_by_page[page_index].append(source_index)
            block_specs.append((source_index, item, page_index, order_in_page))

        typed_annotations = (
            _bind_typed_annotations(
                typed_pages=typed_pages,
                content_indices_by_page=content_indices_by_page,
                content_items=content_items,
            )
            if len(typed_pages) == len(page_sizes)
            else {}
        )

        blocks_by_page: list[list[ProviderBlock]] = [[] for _ in page_sizes]
        for source_index, item, page_index, order_in_page in block_specs:
            provider_type = item.get("type")
            if not isinstance(provider_type, str) or not provider_type:
                raise ParserOutputContractError(
                    f"MinerU content-list item {source_index} has no type"
                )
            raw_item_json = _canonical_item_json(item, source_index=source_index)
            payloads = _provider_payloads(
                item,
                provider_type=provider_type,
                source_index=source_index,
            )
            image_roles = _referenced_image_roles(
                item=item,
                source_index=source_index,
                tree=tree,
                artifact_root=artifact_root,
                artifacts_by_relative=artifacts_by_relative,
            )
            blocks_by_page[page_index].append(
                ProviderBlock(
                    source_index=source_index,
                    page_index=page_index,
                    order_in_page=order_in_page,
                    provider_type=provider_type,
                    typed_annotation=typed_annotations.get(source_index),
                    provider_level=_provider_level(item, source_index=source_index),
                    bbox=_bbox_or_none(item.get("bbox"), strict=False),
                    payloads=payloads,
                    referenced_artifact_roles=image_roles,
                    raw_item_json=raw_item_json,
                    raw_item_sha256=_sha256(raw_item_json.encode("utf-8")),
                )
            )

        physical_table_segments = _physical_table_segments(
            middle_pages=middle_pages,
            page_sizes=page_sizes,
            artifacts_by_relative=artifacts_by_relative,
        )
        pages = tuple(
            ProviderPage(
                page_index=page_index,
                page_size=page_size,
                blocks=tuple(blocks_by_page[page_index]),
            )
            for page_index, page_size in enumerate(page_sizes)
        )
        tree.verify_unchanged()
        return PinnedArtifactReadResult(
            document=ProviderDocument(
                source_pdf_sha256=source_pdf_sha256,
                parser_version=parser_identity[0],
                backend=parser_identity[1],
                effort=parser_identity[2],
                ocr_enabled=ocr_enabled,
                pages=pages,
                physical_table_segments=physical_table_segments,
                artifacts=artifacts_tuple,
                bundle_sha256=provider_artifact_bundle_sha256(artifacts_tuple),
            ),
            artifact_root_relpath=artifact_root,
        )


def _locate_content_list_pinned(tree: PinnedArtifactTree) -> PinnedArtifactFile:
    candidates = tuple(
        file
        for file in tree.files
        if file.relative_path.name.endswith("_content_list.json")
        and not file.relative_path.name.endswith("_content_list_v2.json")
    )
    if len(candidates) != 1:
        raise ParserOutputContractError(
            "MinerU output must contain exactly one content_list artifact"
        )
    return candidates[0]


def _artifact_roles_pinned(
    *,
    artifact_root: PurePosixPath,
    files: tuple[PinnedArtifactFile, ...],
    explicit_role_paths: Mapping[str, PurePosixPath],
) -> dict[str, str]:
    explicit_by_relative = {
        _relative_to(path, artifact_root).as_posix(): role
        for role, path in explicit_role_paths.items()
    }
    relative_paths = {
        _relative_to(file.relative_path, artifact_root).as_posix() for file in files
    }
    if not set(explicit_by_relative).issubset(relative_paths):
        raise ParserOutputContractError("MinerU artifact bundle is incomplete")
    roles: dict[str, str] = {}
    next_sidecar = 0
    for relative in sorted(relative_paths):
        explicit_role = explicit_by_relative.get(relative)
        if explicit_role is not None:
            roles[relative] = explicit_role
            continue
        roles[relative] = f"sidecar_{next_sidecar:06d}"
        next_sidecar += 1
    return roles


def _artifact_record_pinned(
    *,
    role: str,
    file: PinnedArtifactFile,
    tree: PinnedArtifactTree,
    artifact_root: PurePosixPath,
) -> ProviderArtifact:
    if role == "markdown":
        tree.validate_utf8(file.relative_path, label=role)
    return ProviderArtifact(
        role=role,
        relative_path=_relative_to(file.relative_path, artifact_root).as_posix(),
        sha256=file.sha256,
        size_bytes=file.size_bytes,
        media_type=_artifact_media_type(
            role=role,
            leading_bytes=file.leading_bytes,
        ),
    )


def _artifact_media_type(*, role: str, leading_bytes: bytes) -> str:
    if role in _PARSED_JSON_ROLES:
        return "application/json"
    if role == "markdown":
        return "text/markdown"
    if leading_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    if leading_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if leading_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if leading_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(leading_bytes) >= 12
        and leading_bytes[:4] == b"RIFF"
        and leading_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"
    return "application/octet-stream"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _object_list(value: object, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ParserOutputContractError(f"MinerU {label} must be an array of objects")
    return value


def _page_object_lists(
    value: object,
    *,
    label: str,
) -> list[list[dict[str, Any]]]:
    if not isinstance(value, list):
        raise ParserOutputContractError(f"MinerU {label} must be page grouped")
    pages: list[list[dict[str, Any]]] = []
    for page in value:
        if not isinstance(page, list) or not all(
            isinstance(block, dict) for block in page
        ):
            raise ParserOutputContractError(
                f"MinerU {label} pages must contain objects"
            )
        pages.append(page)
    return pages


def _object_value(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParserOutputContractError(f"MinerU {label} must be an object")
    return value


def _middle_document(
    payload: object,
) -> tuple[
    tuple[tuple[float, float], ...],
    tuple[str, str, str],
    bool,
    list[dict[str, Any]],
]:
    middle = _object_value(payload, label="middle_json")
    version = middle.get("_version_name")
    backend = middle.get("_backend")
    effort = middle.get("_effort")
    if not all(isinstance(value, str) for value in (version, backend, effort)):
        raise ParserOutputContractError(
            "MinerU parser identity must contain text values"
        )
    assert isinstance(version, str)
    assert isinstance(backend, str)
    assert isinstance(effort, str)
    if (version, backend, effort) != (
        _EXPECTED_VERSION,
        _EXPECTED_BACKEND,
        _EXPECTED_EFFORT,
    ):
        raise ParserOutputContractError(
            "MinerU artifact identity is not exact 3.4.4 Hybrid-medium"
        )
    ocr_enabled = middle.get("_ocr_enable")
    if not isinstance(ocr_enabled, bool):
        raise ParserOutputContractError("MinerU _ocr_enable must be boolean")
    pdf_info = middle.get("pdf_info")
    if (
        not isinstance(pdf_info, list)
        or not pdf_info
        or not all(isinstance(page, dict) for page in pdf_info)
    ):
        raise ParserOutputContractError("MinerU middle_json must contain PDF pages")
    page_sizes: list[tuple[float, float]] = []
    for expected_page, raw_page in enumerate(pdf_info):
        if not isinstance(raw_page, dict) or raw_page.get("page_idx") != expected_page:
            raise ParserOutputContractError(
                "MinerU middle_json pages must be contiguous and zero-based"
            )
        raw_size = raw_page.get("page_size")
        if not isinstance(raw_size, list) or len(raw_size) != 2:
            raise ParserOutputContractError("MinerU middle_json page_size is invalid")
        width = _positive_number(raw_size[0], field="page width")
        height = _positive_number(raw_size[1], field="page height")
        page_sizes.append((width, height))
    return tuple(page_sizes), (version, backend, effort), ocr_enabled, pdf_info


def _positive_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParserOutputContractError(f"MinerU {field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ParserOutputContractError(f"MinerU {field} must be positive")
    return result


def _page_index(item: Mapping[str, object], *, page_count: int) -> int:
    value = item.get("page_idx")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParserOutputContractError(
            "MinerU content-list page_idx must be an integer"
        )
    if value < 0 or value >= page_count:
        raise ParserOutputContractError("MinerU content-list page_idx is out of range")
    return value


def _provider_level(
    item: Mapping[str, object],
    *,
    source_index: int,
) -> int | None:
    value = item.get("text_level")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ParserOutputContractError(
            f"MinerU content-list item {source_index} has an invalid text_level"
        )
    return value


def _bbox_or_none(value: object, *, strict: bool) -> ProviderBBox | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        if strict:
            raise ParserOutputContractError("MinerU bbox must contain four numbers")
        return None
    values = list(value)
    if len(values) != 4 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in values
    ):
        if strict:
            raise ParserOutputContractError("MinerU bbox must contain four numbers")
        return None
    try:
        return ProviderBBox(*(float(item) for item in values))
    except ValueError:
        if strict:
            raise ParserOutputContractError("MinerU bbox is outside its valid range")
        return None


def _bind_typed_annotations(
    *,
    typed_pages: list[list[dict[str, Any]]],
    content_indices_by_page: list[list[int]],
    content_items: list[dict[str, Any]],
) -> dict[int, str]:
    annotations: dict[int, str] = {}
    for page_index, source_indices in enumerate(content_indices_by_page):
        typed_blocks = typed_pages[page_index]
        if len(source_indices) != len(typed_blocks):
            continue
        for source_index, typed_block in zip(source_indices, typed_blocks, strict=True):
            typed_type = typed_block.get("type")
            if not isinstance(typed_type, str) or not typed_type:
                continue
            primary_item = content_items[source_index]
            primary_type = primary_item.get("type")
            if not isinstance(primary_type, str) or not _types_are_compatible(
                primary_type=primary_type,
                typed_type=typed_type,
            ):
                continue
            primary_bbox = _bbox_or_none(primary_item.get("bbox"), strict=False)
            typed_bbox = _bbox_or_none(typed_block.get("bbox"), strict=False)
            if (
                primary_bbox is None
                or typed_bbox is None
                or primary_bbox.as_tuple() != typed_bbox.as_tuple()
            ):
                continue
            annotations[source_index] = typed_type
    return annotations


def _types_are_compatible(*, primary_type: str, typed_type: str) -> bool:
    return (
        primary_type == typed_type
        or (
            primary_type,
            typed_type,
        )
        in _COMPATIBLE_TYPED_ANNOTATIONS
    )


def _provider_payloads(
    item: Mapping[str, object],
    *,
    provider_type: str,
    source_index: int,
) -> tuple[ProviderPayload, ...]:
    try:
        scalar_fields, sequence_fields = provider_payload_field_contract(provider_type)
    except ValueError as exc:
        raise ParserOutputContractError(
            f"MinerU item {source_index} has unsupported type {provider_type}"
        ) from exc
    allowed_fields = frozenset((*scalar_fields, *sequence_fields))
    misplaced_fields = sorted(
        field
        for field in _KNOWN_PAYLOAD_FIELDS
        if field in item and field not in allowed_fields
    )
    if misplaced_fields:
        raise ParserOutputContractError(
            f"MinerU item {source_index} fields are invalid for type "
            f"{provider_type}: {', '.join(misplaced_fields)}"
        )
    payloads: list[ProviderPayload] = []
    for field in scalar_fields:
        if field not in item or item[field] is None:
            continue
        value = item[field]
        if not isinstance(value, str):
            raise ParserOutputContractError(
                f"MinerU item {source_index} field {field} must be text"
            )
        payloads.append(ProviderPayload(field=field, item_index=None, text=value))
    for field in sequence_fields:
        if field not in item or item[field] is None:
            continue
        value = item[field]
        if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
            values = value
        else:
            raise ParserOutputContractError(
                f"MinerU item {source_index} field {field} must be a text array"
            )
        payloads.extend(
            ProviderPayload(field=field, item_index=index, text=text)
            for index, text in enumerate(values)
        )
    return tuple(payloads)


def _referenced_image_roles(
    *,
    item: Mapping[str, object],
    source_index: int,
    tree: PinnedArtifactTree,
    artifact_root: PurePosixPath,
    artifacts_by_relative: Mapping[str, ProviderArtifact],
) -> tuple[str, ...]:
    roles: list[str] = []
    seen_paths: set[str] = set()
    for field in _IMAGE_PATH_FIELDS:
        value = item.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ParserOutputContractError(
                f"MinerU item {source_index} field {field} must be a path"
            )
        pure = PurePosixPath(value)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ParserOutputContractError(
                f"MinerU item {source_index} contains an unsafe image path"
            )
        tree.require_file(artifact_root / pure)
        relative = pure.as_posix()
        if relative in seen_paths:
            continue
        seen_paths.add(relative)
        artifact = artifacts_by_relative.get(relative)
        if artifact is None:
            raise ParserOutputContractError(
                f"MinerU item {source_index} references an unbound image artifact"
            )
        roles.append(artifact.role)
    return tuple(roles)


def _physical_table_segments(
    *,
    middle_pages: list[dict[str, Any]],
    page_sizes: tuple[tuple[float, float], ...],
    artifacts_by_relative: Mapping[str, ProviderArtifact],
) -> tuple[ProviderPhysicalTableSegment, ...]:
    segments: list[ProviderPhysicalTableSegment] = []
    for page_index, middle_page in enumerate(middle_pages):
        preproc_blocks = _object_array_field(
            middle_page,
            field="preproc_blocks",
            page_index=page_index,
        )
        para_blocks = _object_array_field(
            middle_page,
            field="para_blocks",
            page_index=page_index,
        )
        table_order = 0
        for block in preproc_blocks:
            if block.get("type") != "table":
                continue
            provider_index = _nonnegative_integer(
                block.get("index"),
                field=f"middle page {page_index} table index",
            )
            table_spans = _table_spans(block)
            if len(table_spans) != 1:
                raise ParserOutputContractError(
                    "MinerU physical table segment must contain exactly one table span"
                )
            span = table_spans[0]
            html = span.get("html")
            if not isinstance(html, str) or not html:
                raise ParserOutputContractError(
                    "MinerU physical table segment HTML must be non-empty"
                )
            crop_role = _middle_crop_role(
                span=span,
                artifacts_by_relative=artifacts_by_relative,
            )
            raw_segment_json = _canonical_value_json(
                block,
                label=f"middle page {page_index} table {provider_index}",
            )
            segments.append(
                ProviderPhysicalTableSegment(
                    page_index=page_index,
                    order_in_page=table_order,
                    provider_index=provider_index,
                    bbox=_middle_bbox_or_none(
                        block.get("bbox"),
                        page_size=page_sizes[page_index],
                    ),
                    page_local_html=html,
                    crop_artifact_role=crop_role,
                    logical_stream_status=_table_logical_stream_status(
                        preproc_block=block,
                        para_blocks=para_blocks,
                    ),
                    raw_segment_json=raw_segment_json,
                    raw_segment_sha256=_sha256(raw_segment_json.encode("utf-8")),
                )
            )
            table_order += 1
    return tuple(segments)


def _object_array_field(
    value: Mapping[str, object],
    *,
    field: str,
    page_index: int,
) -> list[dict[str, Any]]:
    raw = value.get(field)
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ParserOutputContractError(
            f"MinerU middle page {page_index} field {field} must contain objects"
        )
    return raw


def _nonnegative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ParserOutputContractError(f"MinerU {field} must be non-negative")
    return value


def _table_spans(block: Mapping[str, object]) -> list[dict[str, Any]]:
    raw_blocks = block.get("blocks")
    if not isinstance(raw_blocks, list) or not all(
        isinstance(item, dict) for item in raw_blocks
    ):
        return []
    spans: list[dict[str, Any]] = []
    for raw_block in raw_blocks:
        raw_lines = raw_block.get("lines")
        if not isinstance(raw_lines, list) or not all(
            isinstance(item, dict) for item in raw_lines
        ):
            continue
        for raw_line in raw_lines:
            raw_spans = raw_line.get("spans")
            if not isinstance(raw_spans, list) or not all(
                isinstance(item, dict) for item in raw_spans
            ):
                continue
            spans.extend(span for span in raw_spans if span.get("type") == "table")
    return spans


def _middle_crop_role(
    *,
    span: Mapping[str, object],
    artifacts_by_relative: Mapping[str, ProviderArtifact],
) -> str | None:
    value = span.get("image_path")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ParserOutputContractError("MinerU middle table image_path must be text")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ParserOutputContractError("MinerU middle table image_path is unsafe")
    relative = pure.as_posix() if len(pure.parts) > 1 else f"images/{pure.as_posix()}"
    artifact = artifacts_by_relative.get(relative)
    if artifact is None:
        raise ParserOutputContractError(
            "MinerU middle table image_path is not hash-bound in the artifact bundle"
        )
    return artifact.role


def _middle_bbox_or_none(
    value: object,
    *,
    page_size: tuple[float, float],
) -> ProviderBBox | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    raw = list(value)
    if len(raw) != 4 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw
    ):
        return None
    x0, y0, x1, y1 = (float(item) for item in raw)
    width, height = page_size
    if not (
        all(math.isfinite(item) for item in (x0, y0, x1, y1))
        and 0 <= x0 < x1 <= width
        and 0 <= y0 < y1 <= height
    ):
        return None
    return ProviderBBox(
        x0=x0 / width * 1000.0,
        y0=y0 / height * 1000.0,
        x1=x1 / width * 1000.0,
        y1=y1 / height * 1000.0,
    )


def _table_logical_stream_status(
    *,
    preproc_block: Mapping[str, object],
    para_blocks: list[dict[str, Any]],
) -> PhysicalTableLogicalStatus:
    identity = _middle_block_identity(preproc_block)
    matches = [
        block
        for block in para_blocks
        if block.get("type") == "table" and _middle_block_identity(block) == identity
    ]
    if len(matches) != 1:
        return "unbound"
    match = matches[0]
    if any(
        isinstance(span.get("html"), str) and bool(span.get("html"))
        for span in _table_spans(match)
    ):
        return "retained"
    raw_blocks = match.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        table_bodies = [
            block
            for block in raw_blocks
            if isinstance(block, dict) and block.get("type") == "table_body"
        ]
        if table_bodies and all(
            block.get("lines_deleted") is True and block.get("lines") == []
            for block in table_bodies
        ):
            return "deleted"
    return "unbound"


def _middle_block_identity(block: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        _canonical_value_json(block.get("type"), label="middle block type"),
        _canonical_value_json(block.get("index"), label="middle block index"),
        _canonical_value_json(block.get("bbox"), label="middle block bbox"),
    )


def _canonical_value_json(value: object, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ParserOutputContractError(
            f"MinerU {label} is not canonical JSON"
        ) from exc


def _canonical_item_json(item: Mapping[str, object], *, source_index: int) -> str:
    return _canonical_value_json(item, label=f"item {source_index}")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "MinerUMediumArtifactReader",
    "PinnedArtifactFile",
    "PinnedArtifactReadResult",
    "PinnedArtifactTree",
]
