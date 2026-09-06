"""Offline retirement of explicit exact legacy files, without directory scanning."""

from collections.abc import Callable
import hashlib
import os
from pathlib import Path

from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    _DirectoryChain, _file_flags, _require_regular,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import AtomicPublicationArtifactConflict
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4PreparedExecutionSpec, decode_v4_prepared_execution_spec,
)
from disclosure_anchor.application.ports.v4_execution_spec_catalog import V4ExecutionSpecCatalogReference, V4LegacyExecutionSpecPathPort


class LegacyV4ExecutionSpecRetirer:
    def __init__(self, paths: V4LegacyExecutionSpecPathPort) -> None:
        self._paths = paths

    def retire_exact(
        self, *, reference: V4ExecutionSpecCatalogReference,
        authorize: Callable[[V4PreparedExecutionSpec], None],
    ) -> bool:
        if type(reference) is not V4ExecutionSpecCatalogReference:
            raise ValueError("legacy retirement requires an exact reference")
        relative = self._paths.v4_execution_spec_relpath(spec_sha256=reference.spec_sha256)
        root = self._paths.data_path(Path())
        if relative.is_absolute() or not relative.parts or any(p in {".", ".."} for p in relative.parts):
            raise ValueError("legacy retirement path must be closed relative authority")
        if not root.is_absolute() or self._paths.data_path(relative) != root.joinpath(*relative.parts):
            raise ValueError("legacy retirement path authority drifted")
        # A missing/unmounted data root is not a verified absent legacy file.
        with _DirectoryChain(root_path=root, components=(), create=False, trip=lambda _phase: None) as root_anchor:
            try:
                chain = _DirectoryChain(root_path=root, components=relative.parts[:-1], create=False, trip=lambda _phase: None)
            except FileNotFoundError:
                root_anchor.verify()
                return False
            try:
                root_anchor.verify()
            except BaseException:
                chain.close()
                raise
        with chain:
            chain.verify()
            try:
                fd = os.open(relative.name, _file_flags(), dir_fd=chain.leaf_fd)
            except FileNotFoundError:
                return False
            try:
                identity = _require_regular(os.fstat(fd), root_device=chain.root_device, label="legacy spec")
                if os.fstat(fd).st_size != reference.byte_count:
                    raise AtomicPublicationArtifactConflict("legacy spec byte count drifted")
                payload = bytearray()
                while len(payload) <= reference.byte_count:
                    chunk = os.read(fd, min(65536, reference.byte_count+1-len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) != reference.byte_count or "sha256:"+hashlib.sha256(payload).hexdigest() != reference.spec_sha256:
                    raise AtomicPublicationArtifactConflict("legacy spec exact bytes drifted")
                spec = decode_v4_prepared_execution_spec(bytes(payload))
                authorize(spec)
                chain.verify()
                current = os.stat(relative.name, dir_fd=chain.leaf_fd, follow_symlinks=False)
                if (
                    _require_regular(current, root_device=chain.root_device, label="legacy spec path") != identity
                    or _require_regular(os.fstat(fd), root_device=chain.root_device, label="legacy spec fd") != identity
                ):
                    raise AtomicPublicationArtifactConflict("legacy spec changed before retirement")
                os.unlink(relative.name, dir_fd=chain.leaf_fd)
                chain.fsync_leaf_to_root()
                chain.verify()
                try:
                    os.stat(relative.name, dir_fd=chain.leaf_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return True
                raise AtomicPublicationArtifactConflict("legacy spec reappeared after retirement")
            finally:
                os.close(fd)
