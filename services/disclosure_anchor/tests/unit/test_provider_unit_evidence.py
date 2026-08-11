"""Provider-native public evidence resolution without role/path disclosure."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.api.unit_evidence import read_unit_evidence, unit_evidence_refs
from disclosure_anchor.application.contracts.provider_document import (
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from tests.unit.test_filing_api_units import _settings
from tests.unit.test_provider_unit_builder import _admitted, _visual_only_document


_PNG = b"\x89PNG\r\n\x1a\nprovider-unit-evidence"


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _fixture(root: Path):  # type: ignore[no-untyped-def]
    original = _visual_only_document("f")
    visual = next(item for item in original.artifacts if item.role == "image_0001")
    replacement = replace(
        visual,
        sha256=_sha256(_PNG),
        size_bytes=len(_PNG),
        media_type="image/png",
    )
    artifacts = tuple(
        sorted(
            (replacement if item.role == visual.role else item for item in original.artifacts),
            key=lambda item: item.relative_path,
        )
    )
    document = replace(
        original,
        artifacts=artifacts,
        bundle_sha256=provider_artifact_bundle_sha256(artifacts),
    )
    admitted = _admitted(document)
    draft = build_provider_units(admitted).units[0]
    settings = _settings(root)
    paths = FileStorePathBuilder(settings)
    data_root = settings.disclosure_data_root / "data"
    record_path = data_root / admitted.provider_document_relpath
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = provider_document_envelope_to_bytes(admitted.envelope)
    record_path.write_bytes(record)
    artifact_path = (
        data_root
        / admitted.envelope.parser_artifact_root_relpath
        / replacement.relative_path
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(_PNG)
    locator = provider_unit_locator_to_payload(draft.locator)
    row = {
        "asset_id": "asset_provider",
        "document_id": admitted.envelope.document_id,
        "processing_run_id": admitted.envelope.artifact_owner_processing_run_id,
        "artifact_owner_processing_run_id": (
            admitted.envelope.artifact_owner_processing_run_id
        ),
        "resolved_artifact_owner_processing_run_id": (
            admitted.envelope.artifact_owner_processing_run_id
        ),
        "artifact_owner_document_id": admitted.envelope.document_id,
        "artifact_owner_run_kind": "parse",
        "payload_kind": draft.payload_kind,
        "payload": draft.payload,
        "artifact_locator": locator,
        "provider": admitted.envelope.provider,
        "provider_document_id": admitted.envelope.provider_document_id,
        "security_code": "000001",
        "raw_file_hash": admitted.envelope.input_raw_file_hash,
        "producer_input_raw_file_hash": admitted.envelope.input_raw_file_hash,
        "artifact_owner_input_raw_file_hash": admitted.envelope.input_raw_file_hash,
        "artifact_hash": admitted.provider_document_sha256,
        "producer_artifact_hash": admitted.provider_document_sha256,
    }
    return paths, row, draft, record_path, artifact_path


class ProviderUnitEvidenceTests(unittest.TestCase):
    def test_provider_evidence_is_digest_authorized_and_pathless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, row, draft, _record_path, _artifact_path = _fixture(Path(tmp))
            refs = unit_evidence_refs(
                asset_id=row["asset_id"],
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                artifact_locator=row["artifact_locator"],
            )
            self.assertEqual(len(refs), 1)
            dumped = refs[0].model_dump()
            self.assertNotIn("artifact_role", dumped)
            self.assertNotIn("relpath", dumped)
            digest = refs[0].sha256.removeprefix("sha256:")

            verified = read_unit_evidence(row=row, digest=digest, paths=paths)
            assert verified is not None
            self.assertEqual(verified.content, _PNG)
            self.assertEqual(verified.media_type, "image/png")
            self.assertIsNone(
                read_unit_evidence(row=row, digest="0" * 64, paths=paths)
            )

    def test_provider_record_and_artifact_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, row, _draft, record_path, artifact_path = _fixture(Path(tmp))
            digest = _sha256(_PNG).removeprefix("sha256:")
            original_record = record_path.read_bytes()

            record_path.write_bytes(original_record + b"\n")
            with self.assertRaises(HTTPException) as record_error:
                read_unit_evidence(row=row, digest=digest, paths=paths)
            self.assertEqual(
                record_error.exception.detail["detail"]["reason"],
                "provider_document_hash_mismatch",
            )

            record_path.write_bytes(original_record)
            artifact_path.write_bytes(_PNG[:-1] + b"x")
            with self.assertRaises(HTTPException) as artifact_error:
                read_unit_evidence(row=row, digest=digest, paths=paths)
            self.assertEqual(
                artifact_error.exception.detail["detail"]["reason"],
                "evidence_artifact_hash_mismatch",
            )

    def test_unknown_locator_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, row, _draft, _record_path, _artifact_path = _fixture(Path(tmp))
            locator = dict(row["artifact_locator"])
            locator["contract_version"] = "unknown.v1"
            row["artifact_locator"] = locator

            with self.assertRaises(HTTPException) as caught:
                unit_evidence_refs(
                    asset_id=row["asset_id"],
                    payload_kind=row["payload_kind"],
                    payload=row["payload"],
                    artifact_locator=row["artifact_locator"],
                )
            self.assertEqual(
                caught.exception.detail["detail"]["reason"],
                "unit_evidence_locator_invalid",
            )
            self.assertIsNotNone(paths)


if __name__ == "__main__":
    unittest.main()
