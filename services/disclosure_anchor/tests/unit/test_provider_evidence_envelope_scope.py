"""Independent real-file checks for document-scoped provider evidence reuse."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from disclosure_anchor.api import unit_evidence as api
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact, provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_from_bytes, provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitEvidenceArtifact, provider_unit_locator_from_payload,
    provider_unit_locator_to_payload,
)
from tests.unit.test_provider_unit_evidence import _fixture


_IMAGES = (
    ("e_images/first.png", b"\x89PNG\r\n\x1a\nfirst-distinct-evidence", "image/png"),
    ("e_images/second.jpg", b"\xff\xd8\xffsecond-distinct-evidence", "image/jpeg"),
    ("e_images/third.png", b"\x89PNG\r\n\x1a\nthird-distinct-evidence", "image/png"),
)


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _image_fixture(root):
    paths, template, _, record, _ = _fixture(root)
    envelope = provider_document_envelope_from_bytes(record.read_bytes())
    # Keep the original image used by the original provider block. Add three
    # independent referenced payloads; no successful hash/decoder is mocked.
    artifacts = tuple(sorted((*envelope.provider_document.artifacts, *(
        ProviderArtifact(role=f"image_r13_{i}", relative_path=name, sha256=_sha(raw),
                         size_bytes=len(raw), media_type=media)
        for i, (name, raw, media) in enumerate(_IMAGES)
    )), key=lambda item: item.relative_path))
    document = replace(envelope.provider_document, artifacts=artifacts,
                       bundle_sha256=provider_artifact_bundle_sha256(artifacts))
    envelope = replace(envelope, provider_document=document)
    exact = provider_document_envelope_to_bytes(envelope)
    record.write_bytes(exact)
    rows, files = [], []
    locator = provider_unit_locator_from_payload(template["artifact_locator"])
    for i, (name, raw, media) in enumerate(_IMAGES):
        image = paths.data_path(Path(envelope.parser_artifact_root_relpath) / name)
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(raw)
        files.append(image)
        bound = replace(locator, unit_index=i, provider_document_sha256=_sha(exact),
                        evidence_artifacts=(ProviderUnitEvidenceArtifact(_sha(raw), len(raw), media),))
        rows.append({**template, "asset_id": f"asset_scope_{i}",
                     "artifact_hash": _sha(exact), "producer_artifact_hash": _sha(exact),
                     "artifact_locator": provider_unit_locator_to_payload(bound)})
    return paths, rows, record, files


class ProviderEvidenceEnvelopeScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths, self.rows, self.record, self.files = _image_fixture(Path(self.directory.name))

    def _scope(self):
        return api.read_provider_envelope(owner=api.unit_evidence_owner(self.rows[0]), paths=self.paths)

    def _read(self, index=0, *, row=None, **kwargs):
        return api.read_unit_evidence(row=self.rows[index] if row is None else row,
            digest=_sha(_IMAGES[index][1])[7:], paths=self.paths, **kwargs)

    def test_three_distinct_images_reuse_one_envelope_but_read_every_payload(self):
        calls, decoded = [], []
        read = api._read_data_bytes
        decode = api.provider_document_envelope_from_bytes

        def observed(paths, relpath, **kwargs):
            calls.append(Path(relpath))
            return read(paths, relpath, **kwargs)

        def decoded_real(raw):
            decoded.append(len(raw))
            return decode(raw)

        with patch.object(api, "_read_data_bytes", observed), patch.object(
            api, "provider_document_envelope_from_bytes", decoded_real,
        ):
            scope = self._scope()
            for i, (_, raw, media) in enumerate(_IMAGES):
                result = self._read(i, envelope=scope)
                self.assertEqual((result.content, result.sha256, result.media_type), (raw, _sha(raw), media))
            # A fourth logical request still rereads the image; no payload cache.
            self.assertEqual(self._read(envelope=scope).content, _IMAGES[0][1])
        self.assertEqual(len(set(_sha(raw) for _, raw, _ in _IMAGES)), 3)
        self.assertEqual(calls.count(scope.record_relpath), 1)
        self.assertEqual(decoded, [self.record.stat().st_size])
        for i, image in enumerate(self.files):
            self.assertEqual(calls.count(image.relative_to(self.paths.data_path(Path()))), 2 if i == 0 else 1)

    def test_warm_scope_does_not_authorize_another_unit_or_inconsistent_owner(self):
        scope = self._scope()
        self._read(envelope=scope)
        with patch.object(api, "_read_data_bytes", side_effect=AssertionError("unauthorized IO")):
            self.assertIsNone(api.read_unit_evidence(row=self.rows[1], digest=_sha(_IMAGES[0][1])[7:],
                                                    paths=self.paths, envelope=scope))
            changes = (
                {"resolved_artifact_owner_processing_run_id": "another-run"},
                {"artifact_owner_document_id": "another-document"},
                {"artifact_owner_run_kind": "unit_rebuild"},
                {"producer_artifact_hash": "sha256:" + "b" * 64},
                {"artifact_owner_input_raw_file_hash": "sha256:" + "b" * 64},
                {"provider": "another-provider"},
                {"security_code": "000002"},
                {"provider_document_id": "another-provider-document"},
                {"document_id": "another-document", "artifact_owner_document_id": "another-document"},
                {"artifact_owner_processing_run_id": "another-run",
                 "resolved_artifact_owner_processing_run_id": "another-run"},
            )
            for change in changes:
                with self.subTest(change=change), self.assertRaises(HTTPException):
                    self._read(row={**self.rows[0], **change}, envelope=scope)
        # Same image can be requested by another explicitly authorized Unit.
        authorized = {**self.rows[0], "asset_id": "second-authorized-unit"}
        self.assertEqual(self._read(row=authorized, envelope=scope).content, _IMAGES[0][1])

    def test_locator_descriptor_still_binds_size_media_and_record_hash(self):
        scope = self._scope()
        for field, value in (("size_bytes", len(_IMAGES[0][1]) + 1), ("media_type", "image/jpeg")):
            row = deepcopy(self.rows[0])
            row["artifact_locator"]["evidence_artifacts"][0][field] = value
            with self.subTest(field=field), patch.object(api, "_read_data_bytes", side_effect=AssertionError("descriptor IO")):
                with self.assertRaises(HTTPException) as caught:
                    self._read(row=row, envelope=scope)
                self.assertEqual(caught.exception.detail["detail"]["reason"], "evidence_artifact_not_in_provider_document")
        row = deepcopy(self.rows[0])
        row["artifact_locator"]["provider_document_sha256"] = "sha256:" + "e" * 64
        with self.assertRaises(HTTPException) as caught:
            self._read(row=row, envelope=scope)
        self.assertEqual(caught.exception.detail["detail"]["reason"], "provider_document_hash_mismatch")

    def test_warm_scope_never_caches_payload_or_bypasses_root_containment(self):
        scope = self._scope()
        original = _IMAGES[0][1]
        self._read(envelope=scope)
        for raw, reason in ((original[:-1] + b"X", "evidence_artifact_hash_mismatch"),
                            (original[:-1], "evidence_artifact_size_mismatch")):
            self.files[0].write_bytes(raw)
            with self.subTest(reason=reason), self.assertRaises(HTTPException) as caught:
                self._read(envelope=scope)
            self.assertEqual(caught.exception.detail["detail"]["reason"], reason)
        self.files[0].write_bytes(original)
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside) / "foreign.png"
            foreign.write_bytes(original)
            self.files[0].unlink()
            self.files[0].symlink_to(foreign)
            with self.assertRaises(HTTPException) as caught:
                self._read(envelope=scope)
            self.assertEqual(caught.exception.detail["detail"]["reason"], "evidence_artifact_path_invalid")

    def test_sealed_envelope_size_precedes_payload_io_and_media_is_verified(self):
        owner = api.unit_evidence_owner(self.rows[0])
        exact_size = self.record.stat().st_size
        with patch.object(Path, "read_bytes", side_effect=AssertionError("oversize envelope payload read")):
            with self.assertRaises(HTTPException) as caught:
                api.read_provider_envelope(owner=owner, paths=self.paths, expected_byte_count=exact_size - 1)
        # Frozen API preserves the existing envelope hash-mismatch reason,
        # including its new pre-read sealed-size rejection.
        self.assertEqual(caught.exception.detail["detail"]["reason"], "provider_document_hash_mismatch")
        scope = api.read_provider_envelope(owner=owner, paths=self.paths, expected_byte_count=exact_size)
        # The envelope and Unit consistently declare JPEG for actual PNG bytes.
        # Hash and size still match, so only actual content/media validation wins.
        items = tuple(replace(item, media_type="image/jpeg") if item.relative_path == _IMAGES[0][0] else item
                      for item in scope.envelope.provider_document.artifacts)
        document = replace(scope.envelope.provider_document, artifacts=items,
                           bundle_sha256=provider_artifact_bundle_sha256(items))
        envelope = replace(scope.envelope, provider_document=document)
        raw = provider_document_envelope_to_bytes(envelope)
        self.record.write_bytes(raw)
        row = deepcopy(self.rows[0])
        row.update(artifact_hash=_sha(raw), producer_artifact_hash=_sha(raw))
        row["artifact_locator"]["provider_document_sha256"] = _sha(raw)
        row["artifact_locator"]["evidence_artifacts"][0]["media_type"] = "image/jpeg"
        scope = api.read_provider_envelope(owner=api.unit_evidence_owner(row), paths=self.paths,
                                           expected_byte_count=len(raw))
        with self.assertRaises(HTTPException) as caught:
            self._read(row=row, envelope=scope)
        self.assertEqual(caught.exception.detail["detail"]["reason"], "evidence_artifact_media_type_mismatch")

    def test_default_route_reloads_record_and_loader_does_not_accept_stale_hash(self):
        calls = []
        read = api._read_data_bytes

        def observed(paths, relpath, **kwargs):
            calls.append(Path(relpath))
            return read(paths, relpath, **kwargs)

        with patch.object(api, "_read_data_bytes", observed):
            self._read()
            self._read()
        record_relpath = self.record.relative_to(self.paths.data_path(Path()))
        self.assertEqual(calls.count(record_relpath), 2)
        original = self.record.read_bytes()
        self.record.write_bytes(original + b"\n")
        for invoke in (self._scope, self._read):
            with self.subTest(invoke=invoke.__name__), self.assertRaises(HTTPException) as caught:
                invoke()
            self.assertEqual(caught.exception.detail["detail"]["reason"], "provider_document_hash_mismatch")
        self.record.write_bytes(original)
        self.assertEqual(self._read(envelope=self._scope()).content, _IMAGES[0][1])


if __name__ == "__main__":
    unittest.main()
