from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.storage import (
    published_parser_output_verifier_v4 as verifier_module,
)
from disclosure_anchor.adapters.storage.published_parser_output_verifier_v4 import (
    PublishedParserOutputVerifierV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalOutputFileV4,
    local_output_files_sha256_v4,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath


class PublishedParserOutputVerifierV4Tests(unittest.TestCase):
    def test_exact_nested_inventory_passes_and_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            relroot = Path("parser_artifacts/cninfo/doc/run")
            values = {
                "manifest.json": b"{}",
                "nested/content.json": b"[]",
            }
            for relpath, payload in values.items():
                path = root / relroot / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            files = tuple(
                LocalOutputFileV4(
                    relpath=relpath,
                    sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
                    byte_count=len(payload),
                )
                for relpath, payload in sorted(values.items())
            )
            verifier = PublishedParserOutputVerifierV4(  # type: ignore[arg-type]
                _Paths(root)
            )
            arguments = {
                "published_relpath": relroot.as_posix(),
                "expected_inventory_sha256": local_output_files_sha256_v4(files),
                "expected_file_count": len(files),
                "expected_byte_count": sum(item.byte_count for item in files),
            }

            verifier.verify_published(**arguments)
            (root / relroot / "manifest.json").write_bytes(b"different")
            with self.assertRaises(ParserOutputContractError):
                verifier.verify_published(**arguments)

    def test_symlink_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            relroot = Path("parser_artifacts/cninfo/doc/run")
            output = root / relroot
            output.mkdir(parents=True)
            target = root / "target"
            target.write_bytes(b"{}")
            (output / "manifest.json").symlink_to(target)
            verifier = PublishedParserOutputVerifierV4(  # type: ignore[arg-type]
                _Paths(root)
            )

            with self.assertRaises(ParserOutputContractError):
                verifier.verify_published(
                    published_relpath=relroot.as_posix(),
                    expected_inventory_sha256="sha256:" + "a" * 64,
                    expected_file_count=1,
                    expected_byte_count=2,
                )

    def test_nested_equal_length_write_after_cached_inventory_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            relroot = Path("parser_artifacts/cninfo/doc/run")
            artifact = root / relroot / "nested/content.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"[]")
            files = (
                LocalOutputFileV4(
                    relpath="nested/content.json",
                    sha256="sha256:" + hashlib.sha256(b"[]").hexdigest(),
                    byte_count=2,
                ),
            )
            expected = local_output_files_sha256_v4(files)
            original = verifier_module.local_output_files_sha256_v4
            mutated = False

            def mutate_after_hash(values: tuple[LocalOutputFileV4, ...]) -> str:
                nonlocal mutated
                digest = original(values)
                if not mutated:
                    mutated = True
                    fd = artifact.open("r+b")
                    try:
                        fd.write(b"{}")
                        fd.flush()
                        os.fsync(fd.fileno())
                    finally:
                        fd.close()
                return digest

            verifier = PublishedParserOutputVerifierV4(  # type: ignore[arg-type]
                _Paths(root)
            )
            with mock.patch.object(
                verifier_module,
                "local_output_files_sha256_v4",
                side_effect=mutate_after_hash,
            ):
                with self.assertRaises(ParserOutputContractError):
                    verifier.verify_published(
                        published_relpath=relroot.as_posix(),
                        expected_inventory_sha256=expected,
                        expected_file_count=1,
                        expected_byte_count=2,
                    )


if __name__ == "__main__":
    unittest.main()
