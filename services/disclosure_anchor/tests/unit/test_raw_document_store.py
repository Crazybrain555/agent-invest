import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.domain.errors import InvalidRawDocumentError
from disclosure_anchor.settings import Settings


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


class RawDocumentStoreTests(unittest.TestCase):
    def test_put_verify_and_reuse_existing_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            input_file = Path(tmp) / "sample.pdf"
            input_file.write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")

            first = store.put_raw_document(
                provider="local",
                security_code="002484",
                year=2025,
                provider_document_id="local-001",
                input_file=input_file,
            )
            self.assertTrue(first.created)
            self.assertEqual(first.relpath.name, first.raw_file_hash.replace(":", "_") + ".pdf")
            self.assertTrue((settings.disclosure_data_root / "data" / first.relpath).is_file())

            verification = store.verify_raw_document(
                relpath=first.relpath, expected_hash=first.raw_file_hash
            )
            self.assertTrue(verification.ok)

            second = store.put_raw_document(
                provider="local",
                security_code="002484",
                year=2025,
                provider_document_id="local-001",
                input_file=input_file,
            )
            self.assertFalse(second.created)
            self.assertEqual(second.relpath, first.relpath)
            self.assertEqual(second.raw_file_hash, first.raw_file_hash)

    def test_rejects_non_pdf_before_raw_archive_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            input_file = Path(tmp) / "not.pdf"
            input_file.write_bytes(b"not a pdf")

            with self.assertRaises(InvalidRawDocumentError):
                store.put_raw_document(
                    provider="local",
                    security_code="002484",
                    year=2025,
                    provider_document_id="local-002",
                    input_file=input_file,
                )

            raw_root = settings.disclosure_data_root / "data" / "raw_documents"
            self.assertFalse(
                any(path.is_file() for path in raw_root.rglob("*"))
                if raw_root.exists()
                else False
            )

    def test_quarantine_copies_input_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            input_file = Path(tmp) / "bad.pdf"
            input_file.write_bytes(b"bad")

            result = store.quarantine_raw_document(
                provider="local",
                provider_document_id="local-003",
                input_file=input_file,
                reason="invalid_raw_document",
            )

            self.assertTrue(result.path.is_file())
            self.assertEqual(result.path.read_bytes(), b"bad")
            self.assertTrue(result.path.with_suffix(result.path.suffix + ".json").is_file())


if __name__ == "__main__":
    unittest.main()
