import json
import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.domain.errors import PathSafetyError
from disclosure_anchor.settings import Settings


def _settings(root: Path) -> Settings:
    return Settings(
        disclosure_data_root=root / "service",
        disclosure_shared_root=root / "shared",
        disclosure_runtime_root=root / "service" / "runtime",
        mineru_model_cache=root / "shared" / "model_cache" / "mineru",
        hf_home=root / "shared" / "model_cache" / "huggingface",
        modelscope_cache=root / "shared" / "model_cache" / "modelscope",
    )


class ArtifactStoreTests(unittest.TestCase):
    def test_write_json_atomic_returns_hash_and_writes_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = FileStorePathBuilder(_settings(Path(tmp)))
            store = ArtifactStore(paths)

            result = store.write_json_atomic(
                relpath=Path("derived/sample/normalized_ir.v2.json"),
                payload={"contract_version": "normalized_ir.v2", "items": [1, 2]},
            )

            final_path = paths.data_path(result.relpath)
            self.assertTrue(final_path.is_file())
            self.assertTrue(result.artifact_hash.startswith("sha256:"))
            self.assertGreater(result.byte_count, 0)
            self.assertEqual(json.loads(final_path.read_text("utf-8"))["items"], [1, 2])
            self.assertFalse(list(final_path.parent.glob("*.tmp")))

    def test_rejects_unsafe_relpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(FileStorePathBuilder(_settings(Path(tmp))))
            with self.assertRaises(PathSafetyError):
                store.write_text_atomic(relpath=Path("../escape.txt"), text="bad")


if __name__ == "__main__":
    unittest.main()
