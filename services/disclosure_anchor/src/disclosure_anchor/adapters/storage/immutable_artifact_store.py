"""No-replace durable writer for transaction-P publication artifacts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import NoReturn

from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict,
    AtomicPublicationArtifactReadinessError,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.domain.ids import new_ulid


_FullIdentity = tuple[int, int, int, int, int, int, int, int]
_StableIdentity = tuple[int, int, int, int]
_PATH_CONFLICT_ERRNOS = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR})


def _raise_path_operation_error(
    exc: OSError,
    *,
    conflict_message: str,
    readiness_message: str,
) -> NoReturn:
    if exc.errno in _PATH_CONFLICT_ERRNOS:
        raise AtomicPublicationArtifactConflict(conflict_message) from exc
    raise AtomicPublicationArtifactReadinessError(readiness_message) from exc


def _full_identity(observed: os.stat_result) -> _FullIdentity:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_uid,
        observed.st_nlink,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _stable_identity(observed: os.stat_result) -> _StableIdentity:
    return observed.st_dev, observed.st_ino, observed.st_mode, observed.st_uid


def _require_platform_flags() -> None:
    missing = tuple(
        name
        for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")
        if not hasattr(os, name)
    )
    if missing:
        raise AtomicPublicationArtifactReadinessError(
            "immutable artifact storage is unsupported: missing "
            + ", ".join(missing)
        )


def _directory_flags() -> int:
    _require_platform_flags()
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
        | os.O_NONBLOCK
    )


def _file_flags() -> int:
    _require_platform_flags()
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _require_directory(
    observed: os.stat_result,
    *,
    root_device: int,
    label: str,
) -> _StableIdentity:
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != root_device
        or observed.st_uid != os.getuid()
        or observed.st_nlink < 1
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise AtomicPublicationArtifactConflict(
            f"immutable artifact {label} identity drifted"
        )
    return _stable_identity(observed)


def _require_regular(
    observed: os.stat_result,
    *,
    root_device: int,
    label: str,
) -> _FullIdentity:
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_dev != root_device
        or observed.st_uid != os.getuid()
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise AtomicPublicationArtifactConflict(
            f"immutable artifact {label} identity drifted"
        )
    return _full_identity(observed)


class _DirectoryChain:
    """Pinned root-to-leaf-parent directory authority."""

    def __init__(
        self,
        *,
        root_path: Path,
        components: Sequence[str],
        create: bool,
        trip: Callable[[str], None],
    ) -> None:
        self._root_path = root_path
        self._components = tuple(components)
        self._trip = trip
        self._fds: list[int] = []
        self._identities: list[_StableIdentity] = []
        self._closed = False
        try:
            self._open_root()
            for component in self._components:
                self._open_child(component, create=create)
        except BaseException:
            self.close()
            raise

    @property
    def leaf_fd(self) -> int:
        if self._closed or not self._fds:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact directory authority is closed"
            )
        return self._fds[-1]

    @property
    def root_device(self) -> int:
        if not self._identities:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact root authority is absent"
            )
        return self._identities[0][0]

    def close(self) -> None:
        if self._closed:
            return
        for descriptor in reversed(self._fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._fds.clear()
        self._closed = True

    def __enter__(self) -> _DirectoryChain:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fsync_leaf_to_root(self) -> None:
        self._require_open()
        try:
            for descriptor, expected in reversed(
                tuple(zip(self._fds, self._identities, strict=True))
            ):
                before = os.fstat(descriptor)
                if _require_directory(
                    before,
                    root_device=self.root_device,
                    label="directory",
                ) != expected:
                    raise AtomicPublicationArtifactConflict(
                        "immutable artifact directory authority changed before fsync"
                    )
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                if _require_directory(
                    after,
                    root_device=self.root_device,
                    label="directory",
                ) != expected:
                    raise AtomicPublicationArtifactConflict(
                        "immutable artifact directory authority changed during fsync"
                    )
        except OSError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact directory cannot be synchronized"
            ) from exc

    def verify(self) -> None:
        self._require_open()
        try:
            displayed = os.stat(self._root_path, follow_symlinks=False)
            if _require_directory(
                displayed,
                root_device=self.root_device,
                label="root",
            ) != self._identities[0]:
                raise AtomicPublicationArtifactConflict(
                    "immutable artifact root path changed"
                )
            for index, component in enumerate(self._components, start=1):
                parent_fd = self._fds[index - 1]
                child_fd = self._fds[index]
                by_name = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                expected = self._identities[index]
                if (
                    _require_directory(
                        by_name,
                        root_device=self.root_device,
                        label="directory entry",
                    )
                    != expected
                    or _require_directory(
                        os.fstat(child_fd),
                        root_device=self.root_device,
                        label="pinned directory",
                    )
                    != expected
                ):
                    raise AtomicPublicationArtifactConflict(
                        "immutable artifact directory path changed"
                    )
        except AtomicPublicationArtifactConflict:
            raise
        except OSError as exc:
            _raise_path_operation_error(
                exc,
                conflict_message="immutable artifact directory path drifted",
                readiness_message=(
                    "immutable artifact directory path cannot be inspected"
                ),
            )

    def _open_root(self) -> None:
        if not self._root_path.is_absolute():
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact data root must be absolute"
            )
        try:
            expected = os.stat(self._root_path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact data root is absent"
            ) from exc
        except OSError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact data root cannot be inspected"
            ) from exc
        root_device = expected.st_dev
        expected_identity = _require_directory(
            expected,
            root_device=root_device,
            label="root",
        )
        try:
            descriptor = os.open(self._root_path, _directory_flags())
        except OSError as exc:
            _raise_path_operation_error(
                exc,
                conflict_message="immutable artifact data root changed while opening",
                readiness_message="immutable artifact data root cannot be pinned",
            )
        try:
            if (
                _require_directory(
                    os.fstat(descriptor),
                    root_device=root_device,
                    label="root",
                )
                != expected_identity
            ):
                raise AtomicPublicationArtifactConflict(
                    "immutable artifact data root changed while opening"
                )
        except BaseException:
            os.close(descriptor)
            raise
        self._fds.append(descriptor)
        self._identities.append(expected_identity)

    def _open_child(self, component: str, *, create: bool) -> None:
        parent_fd = self._fds[-1]
        created = False
        try:
            try:
                expected = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    created = True
                except FileExistsError:
                    pass
                expected = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            expected_identity = _require_directory(
                expected,
                root_device=self.root_device,
                label="directory entry",
            )
            descriptor = os.open(component, _directory_flags(), dir_fd=parent_fd)
        except FileNotFoundError as exc:
            if create:
                raise AtomicPublicationArtifactConflict(
                    "immutable artifact directory changed while opening"
                ) from exc
            raise
        except AtomicPublicationArtifactConflict:
            raise
        except OSError as exc:
            _raise_path_operation_error(
                exc,
                conflict_message="immutable artifact directory cannot be pinned",
                readiness_message=(
                    "immutable artifact directory cannot be opened or created"
                ),
            )
        try:
            if (
                _require_directory(
                    os.fstat(descriptor),
                    root_device=self.root_device,
                    label="pinned directory",
                )
                != expected_identity
            ):
                raise AtomicPublicationArtifactConflict(
                    "immutable artifact directory changed while opening"
                )
            self._fds.append(descriptor)
            self._identities.append(expected_identity)
            if created:
                try:
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise AtomicPublicationArtifactReadinessError(
                        "immutable artifact directory entry cannot be synchronized"
                    ) from exc
                self._trip("after_directory_fsync")
        except BaseException:
            if descriptor not in self._fds:
                os.close(descriptor)
            raise

    def _require_open(self) -> None:
        if self._closed or not self._fds:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact directory authority is closed"
            )


class ImmutableArtifactStore:
    """Publish one immutable regular file below a pinned data-root descriptor."""

    def __init__(
        self,
        paths: FileStorePathPort,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._paths = paths
        self._fault_hook = fault_hook

    def create_or_verify(
        self,
        *,
        relpath: Path,
        payload: bytes,
    ) -> ArtifactWriteResult:
        if type(payload) is not bytes or not payload:
            raise AtomicPublicationArtifactReadinessError(
                "immutable artifact payload must be nonempty bytes"
            )
        expected_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
        chain, final_name = self._open_parent_chain(relpath, create=True)
        temp_name: str | None = None
        temp_cleanup_identity: _StableIdentity | None = None
        temp_sealed_identity: _FullIdentity | None = None
        try:
            existing = self._try_read_at(
                chain.leaf_fd,
                final_name,
                root_device=chain.root_device,
                expected_sha256=expected_sha256,
                expected_byte_count=len(payload),
                max_byte_count=len(payload),
                fsync_file=True,
            )
            if existing is not None:
                chain.fsync_leaf_to_root()
                chain.verify()
                self._require_exact_after_durability(
                    chain=chain,
                    name=final_name,
                    expected_sha256=expected_sha256,
                    expected_byte_count=len(payload),
                )
                return ArtifactWriteResult(
                    relpath=relpath,
                    artifact_hash=expected_sha256,
                    byte_count=len(payload),
                )

            temp_name = f".{final_name}.{new_ulid()}.tmp"
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=chain.leaf_fd,
                )
            except OSError as exc:
                raise AtomicPublicationArtifactReadinessError(
                    "immutable publication temp cannot be created"
                ) from exc
            try:
                temp_observed = os.fstat(temp_fd)
                _require_regular(
                    temp_observed,
                    root_device=chain.root_device,
                    label="temp",
                )
                temp_cleanup_identity = _stable_identity(temp_observed)
                with os.fdopen(temp_fd, "wb") as handle:
                    temp_fd = -1
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                    after_write = os.fstat(handle.fileno())
                    temp_sealed_identity = _require_regular(
                        after_write,
                        root_device=chain.root_device,
                        label="temp",
                    )
            except AtomicPublicationArtifactConflict:
                raise
            except OSError as exc:
                raise AtomicPublicationArtifactReadinessError(
                    "immutable publication temp cannot be synchronized"
                ) from exc
            finally:
                if temp_fd >= 0:
                    os.close(temp_fd)
            self._trip("after_temp_fsync")

            installed = self._rename_no_replace(
                parent_fd=chain.leaf_fd,
                temp_name=temp_name,
                final_name=final_name,
                root_device=chain.root_device,
                expected_temp_identity=temp_sealed_identity,
            )
            if not installed:
                existing = self._try_read_at(
                    chain.leaf_fd,
                    final_name,
                    root_device=chain.root_device,
                    expected_sha256=expected_sha256,
                    expected_byte_count=len(payload),
                    max_byte_count=len(payload),
                    fsync_file=False,
                )
                if existing is None:
                    raise AtomicPublicationArtifactConflict(
                        "immutable publication artifact already has different bytes"
                    )
            self._trip("after_rename")
            self._require_exact_file_fsync(
                chain=chain,
                name=final_name,
                expected_sha256=expected_sha256,
                expected_byte_count=len(payload),
            )
            self._trip("after_final_fsync")
            chain.fsync_leaf_to_root()
            self._trip("after_parent_fsync")
            chain.verify()
            self._require_exact_after_durability(
                chain=chain,
                name=final_name,
                expected_sha256=expected_sha256,
                expected_byte_count=len(payload),
            )
        finally:
            try:
                if temp_name is not None:
                    self._cleanup_temp(
                        parent_fd=chain.leaf_fd,
                        name=temp_name,
                        root_device=chain.root_device,
                        expected_identity=temp_cleanup_identity,
                    )
            finally:
                chain.close()
        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash=expected_sha256,
            byte_count=len(payload),
        )

    def read_exact(
        self,
        *,
        relpath: Path,
        expected_sha256: str,
        expected_byte_count: int,
        max_byte_count: int,
    ) -> bytes:
        try:
            chain, final_name = self._open_parent_chain(relpath, create=False)
        except FileNotFoundError:
            raise AtomicPublicationArtifactReadinessError(
                "immutable publication artifact is absent"
            ) from None
        with chain:
            chain.verify()
            value = self._try_read_at(
                chain.leaf_fd,
                final_name,
                root_device=chain.root_device,
                expected_sha256=expected_sha256,
                expected_byte_count=expected_byte_count,
                max_byte_count=max_byte_count,
                fsync_file=False,
            )
            if value is None:
                raise AtomicPublicationArtifactReadinessError(
                    "immutable publication artifact is absent"
                )
            chain.verify()
            return value

    def _open_parent_chain(
        self,
        relpath: Path,
        *,
        create: bool,
    ) -> tuple[_DirectoryChain, str]:
        components = self._safe_components(relpath)
        root_path = self._paths.data_path(Path())
        final_path = self._paths.data_path(relpath)
        expected_path = root_path.joinpath(*components)
        if (
            not root_path.is_absolute()
            or not final_path.is_absolute()
            or os.path.normpath(os.fspath(final_path))
            != os.path.normpath(os.fspath(expected_path))
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable artifact path authority drifted"
            )
        chain = _DirectoryChain(
            root_path=root_path,
            components=components[:-1],
            create=create,
            trip=self._trip,
        )
        return chain, components[-1]

    @staticmethod
    def _safe_components(relpath: Path) -> tuple[str, ...]:
        components = relpath.parts
        if (
            relpath.is_absolute()
            or not components
            or any(
                not component
                or component in {".", ".."}
                or "/" in component
                or "\\" in component
                or "\x00" in component
                for component in components
            )
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable artifact relative path is unsafe"
            )
        return components

    @staticmethod
    def _try_read_at(
        parent_fd: int,
        name: str,
        *,
        root_device: int,
        expected_sha256: str,
        expected_byte_count: int,
        max_byte_count: int,
        fsync_file: bool,
    ) -> bytes | None:
        if (
            type(expected_byte_count) is not int
            or expected_byte_count < 1
            or type(max_byte_count) is not int
            or max_byte_count < expected_byte_count
        ):
            raise AtomicPublicationArtifactReadinessError(
                "immutable publication artifact byte bounds are invalid"
            )
        try:
            by_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            _raise_path_operation_error(
                exc,
                conflict_message=(
                    "immutable publication artifact identity drifted"
                ),
                readiness_message=(
                    "immutable publication artifact cannot be inspected"
                ),
            )
        expected_identity = _require_regular(
            by_name,
            root_device=root_device,
            label="file",
        )
        if (
            by_name.st_size != expected_byte_count
            or by_name.st_size > max_byte_count
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact identity drifted"
            )
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact changed while opening"
            ) from exc
        except OSError as exc:
            _raise_path_operation_error(
                exc,
                conflict_message=(
                    "immutable publication artifact identity drifted"
                ),
                readiness_message=(
                    "immutable publication artifact cannot be opened"
                ),
            )
        try:
            before = os.fstat(descriptor)
            if (
                _require_regular(
                    before,
                    root_device=root_device,
                    label="pinned file",
                )
                != expected_identity
            ):
                raise AtomicPublicationArtifactConflict(
                    "immutable publication artifact changed while opening"
                )
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            byte_count = 0
            while True:
                read_size = min(
                    1024 * 1024,
                    max_byte_count - byte_count + 1,
                )
                chunk = os.read(descriptor, read_size)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > max_byte_count:
                    raise AtomicPublicationArtifactConflict(
                        "immutable publication artifact exceeds its byte limit"
                    )
                digest.update(chunk)
                chunks.append(chunk)
            if fsync_file:
                os.fsync(descriptor)
            after = os.fstat(descriptor)
            after_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except AtomicPublicationArtifactConflict:
            raise
        except FileNotFoundError as exc:
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact disappeared while read"
            ) from exc
        except OSError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "immutable publication artifact cannot be read or synchronized"
            ) from exc
        finally:
            os.close(descriptor)
        if (
            _full_identity(before) != _full_identity(after)
            or _full_identity(after_name) != _full_identity(after)
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact changed while read"
            )
        exact = b"".join(chunks)
        actual = "sha256:" + digest.hexdigest()
        if (
            byte_count != expected_byte_count
            or len(exact) != expected_byte_count
            or actual != expected_sha256
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact bytes drifted"
            )
        return exact

    def _require_exact_file_fsync(
        self,
        *,
        chain: _DirectoryChain,
        name: str,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> None:
        if (
            self._try_read_at(
                chain.leaf_fd,
                name,
                root_device=chain.root_device,
                expected_sha256=expected_sha256,
                expected_byte_count=expected_byte_count,
                max_byte_count=expected_byte_count,
                fsync_file=True,
            )
            is None
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact disappeared before fsync"
            )

    def _require_exact_after_durability(
        self,
        *,
        chain: _DirectoryChain,
        name: str,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> None:
        if (
            self._try_read_at(
                chain.leaf_fd,
                name,
                root_device=chain.root_device,
                expected_sha256=expected_sha256,
                expected_byte_count=expected_byte_count,
                max_byte_count=expected_byte_count,
                fsync_file=False,
            )
            is None
        ):
            raise AtomicPublicationArtifactConflict(
                "immutable publication artifact disappeared after durability"
            )
        chain.verify()

    def _trip(self, phase: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase)

    @staticmethod
    def _rename_no_replace(
        *,
        parent_fd: int,
        temp_name: str,
        final_name: str,
        root_device: int,
        expected_temp_identity: _FullIdentity | None,
    ) -> bool:
        if expected_temp_identity is None:
            raise AtomicPublicationArtifactReadinessError(
                "immutable publication temp identity is absent"
            )
        try:
            observed = os.stat(
                temp_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AtomicPublicationArtifactReadinessError(
                "immutable publication temp disappeared before rename"
            ) from exc
        observed_identity = _require_regular(
            observed,
            root_device=root_device,
            label="temp",
        )
        if observed_identity != expected_temp_identity:
            raise AtomicPublicationArtifactConflict(
                "immutable publication temp identity drifted"
            )

        libc = ctypes.CDLL(None, use_errno=True)
        source = ctypes.c_char_p(os.fsencode(temp_name))
        target = ctypes.c_char_p(os.fsencode(final_name))
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
                parent_fd,
                source,
                parent_fd,
                target,
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
                parent_fd,
                source,
                parent_fd,
                target,
                ctypes.c_uint(1),  # RENAME_NOREPLACE
            )
        else:
            raise AtomicPublicationArtifactReadinessError(
                "atomic no-replace rename is unavailable"
            )
        if result == 0:
            return True
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            return False
        failure = OSError(error, os.strerror(error), temp_name, final_name)
        raise AtomicPublicationArtifactReadinessError(
            "immutable publication no-replace rename failed"
        ) from failure

    @staticmethod
    def _cleanup_temp(
        *,
        parent_fd: int,
        name: str,
        root_device: int,
        expected_identity: _StableIdentity | None,
    ) -> None:
        if expected_identity is None:
            return
        try:
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            _require_regular(observed, root_device=root_device, label="temp")
            if _stable_identity(observed) != expected_identity:
                return
            os.unlink(name, dir_fd=parent_fd)
        except (FileNotFoundError, AtomicPublicationArtifactConflict):
            return
        except OSError:
            # A uniquely named, non-authoritative temp is safe to retain.
            return


__all__ = ["ImmutableArtifactStore"]
