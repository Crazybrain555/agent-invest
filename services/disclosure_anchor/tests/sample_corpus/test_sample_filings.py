"""Data-level validation against the local CNINFO sample corpus.

The corpus lives under ``tmp/sample_filings`` (git-ignored, machine-local). These
tests skip cleanly when it is absent so CI without the corpus stays green, but
when present they assert (1) the manifest matches the on-disk PDFs and (2) real
provider IDs and content hashes flow safely through ``FileStorePathBuilder``.
"""

import hashlib
import json
import unittest
from pathlib import Path

from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = REPO_ROOT / "tmp" / "sample_filings"
MANIFEST = SAMPLE_ROOT / "manifest.jsonl"

PROVIDER = "cninfo"


def _load_manifest() -> list[dict]:
    entries: list[dict] = []
    with MANIFEST.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _builder() -> FileStorePathBuilder:
    base = REPO_ROOT / "tmp" / "_unused_path_builder_root"
    settings = Settings(
        disclosure_data_root=base / "services" / "disclosure_anchor",
        disclosure_shared_root=base / "shared",
        disclosure_runtime_root=base / "services" / "disclosure_anchor" / "runtime",
        mineru_model_cache=base / "shared" / "model_cache" / "mineru",
        hf_home=base / "shared" / "model_cache" / "huggingface",
        modelscope_cache=base / "shared" / "model_cache" / "modelscope",
    )
    return FileStorePathBuilder(settings)


@unittest.skipUnless(MANIFEST.is_file(), f"sample corpus absent: {MANIFEST}")
class SampleFilingsManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entries = _load_manifest()

    def test_manifest_is_not_empty(self) -> None:
        self.assertGreater(len(self.entries), 0)

    def test_every_entry_points_at_an_existing_pdf(self) -> None:
        for entry in self.entries:
            local_path = REPO_ROOT / entry["local_path"]
            self.assertTrue(local_path.is_file(), entry["local_path"])

    def test_entry_sha256_matches_the_file_on_disk(self) -> None:
        for entry in self.entries:
            local_path = REPO_ROOT / entry["local_path"]
            if not local_path.is_file():
                self.skipTest(f"missing file: {entry['local_path']}")
            self.assertEqual(_sha256(local_path), entry["sha256"], entry["local_path"])


@unittest.skipUnless(MANIFEST.is_file(), f"sample corpus absent: {MANIFEST}")
class SampleFilingsPathBuilderTests(unittest.TestCase):
    """Drive the path builder with real provider IDs and content hashes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.entries = _load_manifest()
        cls.builder = _builder()

    def test_raw_relpaths_are_safe_relative_and_well_shaped(self) -> None:
        seen: dict[Path, str] = {}
        for entry in self.entries:
            textid = entry["cninfo_textid"]
            sha = entry["sha256"]
            relpath = self.builder.raw_document_relpath(
                provider=PROVIDER,
                security_code=entry["seccode"],
                year=entry["announcement_date"][:4],
                provider_document_id=textid,
                raw_file_hash=f"sha256:{sha}",
            )
            self.assertFalse(relpath.is_absolute(), textid)
            self.assertNotIn("..", relpath.parts)
            self.assertEqual(relpath.parts[0], "raw_documents")
            self.assertEqual(relpath.parts[1], PROVIDER)
            self.assertEqual(relpath.parts[2], entry["seccode"])
            self.assertEqual(relpath.parts[3], entry["announcement_date"][:4])
            self.assertEqual(relpath.parts[4], textid)
            self.assertEqual(relpath.name, f"sha256_{sha}.pdf")

            # Distinct documents must not collide on the same relpath.
            if relpath in seen and seen[relpath] != textid:
                self.fail(f"relpath collision: {relpath}")
            seen[relpath] = textid

    def test_raw_relpath_is_deterministic(self) -> None:
        entry = self.entries[0]
        kwargs = dict(
            provider=PROVIDER,
            security_code=entry["seccode"],
            year=entry["announcement_date"][:4],
            provider_document_id=entry["cninfo_textid"],
            raw_file_hash=f"sha256:{entry['sha256']}",
        )
        self.assertEqual(
            self.builder.raw_document_relpath(**kwargs),
            self.builder.raw_document_relpath(**kwargs),
        )


if __name__ == "__main__":
    unittest.main()
