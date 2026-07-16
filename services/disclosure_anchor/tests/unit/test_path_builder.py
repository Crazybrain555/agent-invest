import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.domain.errors import PathSafetyError
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


class PathBuilderTests(unittest.TestCase):
    def test_store_paths_are_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = FileStorePathBuilder(_settings(Path(tmp)))
            digest = "a" * 64

            paths = [
                builder.raw_document_relpath(
                    provider="cninfo",
                    security_code="002484",
                    year=2025,
                    provider_document_id="1225087169",
                    raw_file_hash=f"sha256:{digest}",
                ),
                builder.parser_artifacts_root_relpath(
                    document_id="doc_01K00000000000000000000000",
                    processing_run_id="run_01K0000000000000000000000",
                ),
                builder.normalized_ir_relpath(
                    document_id="doc_01K00000000000000000000000",
                    processing_run_id="run_01K0000000000000000000000",
                ),
                builder.document_units_snapshot_relpath(
                    provider="cninfo",
                    security_code="002484",
                    provider_document_id="1225087169",
                    processing_run_id="run_01K0000000000000000000000",
                ),
            ]

            for path in paths:
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)

    def test_runtime_tmp_path_stays_under_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            builder = FileStorePathBuilder(settings)
            self.assertEqual(builder.runtime_tmp_path(), settings.disclosure_runtime_root / "tmp")
            self.assertEqual(
                builder.runtime_tmp_path("probe.json"),
                settings.disclosure_runtime_root / "tmp" / "probe.json",
            )

    def test_rejects_unsafe_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = FileStorePathBuilder(_settings(Path(tmp)))
            with self.assertRaises(PathSafetyError):
                builder.raw_document_relpath(
                    provider="../cninfo",
                    security_code="002484",
                    year=2025,
                    provider_document_id="1225087169",
                    raw_file_hash="sha256:" + "a" * 64,
                )
            with self.assertRaises(PathSafetyError):
                builder.raw_document_relpath(
                    provider="cninfo",
                    security_code="../002484",
                    year=2025,
                    provider_document_id="1225087169",
                    raw_file_hash="sha256:" + "a" * 64,
                )
            with self.assertRaises(PathSafetyError):
                builder.runtime_tmp_path("../escape")

    def test_raw_document_relpath_uses_sha256_filename_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = FileStorePathBuilder(_settings(Path(tmp)))
            digest = "b" * 64
            relpath = builder.raw_document_relpath(
                provider="cninfo",
                security_code="002484",
                year="2025",
                provider_document_id="1225087169",
                raw_file_hash=f"sha256:{digest}",
            )
            self.assertEqual(
                relpath,
                Path(
                    "raw_documents/cninfo/002484/2025/1225087169/"
                    f"sha256_{digest}.pdf"
                ),
            )
            self.assertEqual(
                builder.data_path(relpath),
                Path(tmp)
                / "services"
                / "disclosure_anchor"
                / "data"
                / relpath,
            )

    def test_provider_document_id_allows_cninfo_filename_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = FileStorePathBuilder(_settings(Path(tmp)))
            digest = "c" * 64
            provider_document_id = (
                "2026-06-18__risk_or_forecast__002484__江海股份："
                "南通江海电容器股份有限公司关于股票交易异常波动的公告__1225376481"
            )

            relpath = builder.raw_document_relpath(
                provider="cninfo",
                security_code="002484",
                year="2026",
                provider_document_id=provider_document_id,
                raw_file_hash=f"sha256:{digest}",
            )

            self.assertIn(provider_document_id, relpath.parts)
            with self.assertRaises(PathSafetyError):
                builder.raw_document_relpath(
                    provider="cninfo",
                    security_code="002484",
                    year="2026",
                    provider_document_id="../" + provider_document_id,
                    raw_file_hash=f"sha256:{digest}",
                )

    def test_phase04_parser_and_normalized_ir_run_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = FileStorePathBuilder(_settings(Path(tmp)))
            artifact_relpath = builder.parser_run_artifacts_relpath(
                provider="cninfo",
                security_code="002484",
                provider_document_id="1225087169",
                processing_run_id="run_01K0000000000000000000000",
            )
            normalized_relpath = builder.normalized_ir_run_relpath(
                provider="cninfo",
                security_code="002484",
                provider_document_id="1225087169",
                processing_run_id="run_01K0000000000000000000000",
            )
            units_relpath = builder.document_units_snapshot_relpath(
                provider="cninfo",
                security_code="002484",
                provider_document_id="1225087169",
                processing_run_id="run_01K0000000000000000000000",
            )
            self.assertEqual(
                artifact_relpath,
                Path(
                    "parser_artifacts/cninfo/002484/1225087169/"
                    "run_01K0000000000000000000000"
                ),
            )
            self.assertEqual(
                normalized_relpath,
                Path(
                    "derived/normalized_ir/cninfo/002484/1225087169/"
                    "run_01K0000000000000000000000/normalized_ir.v3.json"
                ),
            )
            self.assertEqual(
                units_relpath,
                Path(
                    "derived/document_unit_snapshots/cninfo/002484/1225087169/"
                    "run_01K0000000000000000000000/document_units.v1.jsonl"
                ),
            )


if __name__ == "__main__":
    unittest.main()
