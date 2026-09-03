"""Read-only exact inventory verifier for published parser output v4."""

from __future__ import annotations

from pathlib import Path

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    PinnedArtifactTree,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalOutputFileV4,
    local_output_files_sha256_v4,
)
from disclosure_anchor.application.contracts.staged_resource_paths import (
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.domain.errors import ParserOutputContractError


class PublishedParserOutputVerifierV4:
    """Verify the exact tree hash/count/bytes without filesystem mutation."""

    def __init__(self, paths: FileStorePathPort) -> None:
        self._paths = paths

    def verify_published(
        self,
        *,
        published_relpath: str,
        expected_inventory_sha256: str,
        expected_file_count: int,
        expected_byte_count: int,
    ) -> None:
        validate_relative_resource_path_v4(
            published_relpath,
            "published parser output",
        )
        if (
            type(expected_file_count) is not int
            or expected_file_count < 1
            or type(expected_byte_count) is not int
            or expected_byte_count < 1
        ):
            raise ParserOutputContractError(
                "published parser output bounds are invalid"
            )
        root = self._paths.data_path(Path(published_relpath))
        with PinnedArtifactTree.open_path(
            root,
            max_files=expected_file_count,
            max_bytes=expected_byte_count,
        ) as tree:
            files = tuple(
                LocalOutputFileV4(
                    relpath=item.relative_path.as_posix(),
                    sha256=item.sha256,
                    byte_count=item.size_bytes,
                )
                for item in tree.files
            )
            if (
                len(files) != expected_file_count
                or sum(item.byte_count for item in files) != expected_byte_count
                or local_output_files_sha256_v4(files)
                != expected_inventory_sha256
            ):
                raise ParserOutputContractError(
                    "published parser output inventory drifted"
                )
            tree.verify_unchanged()


__all__ = ["PublishedParserOutputVerifierV4"]
