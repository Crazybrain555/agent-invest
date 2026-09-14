"""Image-bearing public confirmation only through the managed scratch runner."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from disclosure_anchor.api import unit_evidence as evidence_api
from disclosure_anchor.adapters.runtime import m6_public_consumer_verifier as consumer_module
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    seal_atomic_publication_request_v4, seal_pre_id_unit_publication_v4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME, LocalMaterializationPayloadFileV4,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact, provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME, provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitEvidenceArtifact, provider_unit_locator_from_payload,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalOutputFileV4, advance_remote_parse_checkpoint_v4,
    build_local_materialization_receipt_v4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    semantic_adjudication_terminal_v1, semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes, content_hash_aggregate, structure_hash_aggregate,
)
import tests.integration._remote_parse_v4_factory as authority_factory
import tests.integration.test_m6_public_consumer as public_fixture


_IMAGES = (
    ("e_images/first.png", b"\x89PNG\r\n\x1a\nfirst-distinct-evidence", "image/png"),
    ("e_images/second.jpg", b"\xff\xd8\xffsecond-distinct-evidence", "image/jpeg"),
    ("e_images/third.png", b"\x89PNG\r\n\x1a\nthird-distinct-evidence", "image/png"),
)


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _with_images(fixture):
    """Extend only the fixture's materialized cycle through real contracts.

    The original source selection and authority remain intact. This helper
    runs before any DB fixture insertion; it never mutates committed rows.
    Its unused prebuilt success/ACK branches are not installed by this suite.
    """
    artifacts = tuple(sorted((*fixture.provider_envelope.provider_document.artifacts, *(
        ProviderArtifact(role=f"image_r13_{i}", relative_path=name, sha256=_sha(raw),
                         size_bytes=len(raw), media_type=media)
        for i, (name, raw, media) in enumerate(_IMAGES)
    )), key=lambda item: item.relative_path))
    document = replace(fixture.provider_envelope.provider_document, artifacts=artifacts,
                       bundle_sha256=provider_artifact_bundle_sha256(artifacts))
    envelope = replace(fixture.provider_envelope, provider_document=document)
    raw_envelope = provider_document_envelope_to_bytes(envelope)
    files = tuple(sorted((*fixture.parser_artifact_files, *((name, raw) for name, raw, _ in _IMAGES))))
    payloads = tuple(sorted((
        LocalMaterializationPayloadFileV4(role="provider_envelope", relpath=PROVIDER_DOCUMENT_FILENAME,
                                         sha256=_sha(raw_envelope), byte_count=len(raw_envelope)),
        *(LocalMaterializationPayloadFileV4(role="parser_artifact", relpath=name,
                                          sha256=_sha(raw), byte_count=len(raw)) for name, raw in files),
    ), key=lambda item: item.relpath))
    byte_count = sum(item.byte_count for item in payloads)
    observations = replace(fixture.materialization_manifest.observations,
                           uncompressed_byte_count=byte_count, temporary_disk_peak_byte_count=2 * byte_count,
                           output_file_count=len(payloads), output_byte_count=byte_count)
    manifest = replace(fixture.materialization_manifest,
                       provider_envelope_sha256=_sha(raw_envelope), provider_envelope_byte_count=len(raw_envelope),
                       observations=observations, payload_files=payloads)
    outputs = tuple(sorted((
        LocalOutputFileV4(relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
                         sha256=manifest.sha256, byte_count=len(manifest.canonical_bytes)),
        *(LocalOutputFileV4(relpath=item.relpath, sha256=item.sha256, byte_count=item.byte_count) for item in payloads),
    ), key=lambda item: item.relpath))
    receipt = build_local_materialization_receipt_v4(
        intent=fixture.materialization_intent, manifest=manifest, source_page_count=2,
        output_files=outputs, provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count=observations.member_count, uncompressed_byte_count=byte_count,
        decoded_byte_count=observations.decoded_byte_count, temporary_disk_peak_byte_count=2 * byte_count,
        file_fsync_completed=True, output_parent_fsync_completed=True, marker_removed=True,
        spool_part_absent=True, spool_part_owner_absent=True, staging_absent=True,
    )
    checkpoint = advance_remote_parse_checkpoint_v4(
        fixture.materializing, state="local_materialized",
        held_resource_credit=replace(fixture.local_materialized.held_resource_credit, output_bytes=receipt.output_byte_count),
        local_materialization_receipt_sha256=receipt.sha256,
    )
    return replace(fixture, provider_envelope=envelope, parser_artifact_files=files,
                   materialization_manifest=manifest, local_materialization_receipt=receipt,
                   local_materialized=checkpoint)


def _image_units_request(original):
    """Two independently sealed Unit rows, with three distinct image refs."""
    template = original.units[0]
    original_locator = provider_unit_locator_from_payload(json.loads(template.canonical_artifact_locator_json))
    units, routes = [], []
    for index, images in enumerate((_IMAGES[:2], _IMAGES[2:]), 1):
        locator = replace(original_locator, unit_index=index - 1, evidence_artifacts=tuple(
            ProviderUnitEvidenceArtifact(_sha(raw), len(raw), media) for _, raw, media in images
        ))
        locator_raw = _canonical(provider_unit_locator_to_payload(locator))
        payload = {"parts": [{"content_artifacts": [{"sha256": _sha(raw), "size_bytes": len(raw), "media_type": media}]}
                             for _, raw, media in images]}
        hashes = compute_unit_hashes(payload_kind="mixed", payload=payload, title=None,
            heading_path=[], semantic_keys=None, section_keys=None, quality_status="ok",
            applicability="applicable", order_index=index)
        values = asdict(template)
        values.pop("routed_draft_sha256")
        values.update(unit_index=index, payload_kind="mixed", canonical_payload_json=_canonical(payload).decode(),
            page_no=index, page_numbers=(index,), section_keys=None,
            canonical_artifact_locator_json=locator_raw.decode(), provider_locator_sha256=_sha(locator_raw),
            content_hash=hashes.content_hash, structure_hash=hashes.structure_hash,
            query_projection_hash=hashes.query_projection_hash)
        unit = seal_pre_id_unit_publication_v4(**values)
        units.append(unit)
        routes.append(replace(original.semantic_route_receipts[0], unit_order_index=index,
                              provider_locator_sha256=unit.provider_locator_sha256,
                              routed_draft_sha256=unit.routed_draft_sha256))
    routes = tuple(routes)
    terminal = semantic_adjudication_terminal_v1(tuple(row.receipt for row in routes))
    projection = json.loads(original.processing_run_projection_json)
    projection.update(unit_count=2, content_hash_aggregate=content_hash_aggregate(tuple(unit.content_hash for unit in units)),
        structure_hash_aggregate=structure_hash_aggregate(tuple(unit.structure_hash for unit in units)),
        semantic_route_receipts_sha256=_sha(semantic_route_receipts_file_bytes_v3(routes)),
        semantic_adjudication_status=terminal.status, semantic_adjudication_summary=terminal.summary,
        semantic_degraded_unit_count=terminal.degraded_unit_count, semantic_failover_group_count=terminal.failover_group_count)
    raw = _canonical(projection)
    return seal_atomic_publication_request_v4(
        identity=original.identity, upstream_evidence=original.upstream_evidence,
        source_page_count=original.source_page_count, processing_run_projection_json=raw.decode(),
        processing_run_projection_sha256=_sha(raw), semantic_route_receipts_contract_version=original.semantic_route_receipts_contract_version,
        semantic_route_receipts=routes, expected_unit_build_status_before=original.expected_unit_build_status_before,
        expected_unit_build_attempt_count_before=original.expected_unit_build_attempt_count_before,
        previous_active_units=original.previous_active_units, previous_active_units_sha256=original.previous_active_units_sha256,
        units=tuple(units), contract_version=original.contract_version,
    )


class M6PublicEvidenceBudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = public_fixture.M6PublicConsumerIntegrationTests()
        self.addCleanup(self.base.doCleanups)
        original_build = authority_factory.build_v4_authority_fixture

        def images_fixture(**kwargs):
            return _with_images(original_build(**kwargs))

        with patch.object(authority_factory, "build_v4_authority_fixture", images_fixture), patch.object(
            public_fixture, "_two_unit_request", _image_units_request,
        ):
            self.base.setUp()
        self.raw_envelope = provider_document_envelope_to_bytes(self.base.fixture.provider_envelope)
        context = self.base.fixture.materialization_intent.provider_envelope_context
        self.record_relpath = self.base.paths.provider_document_relpath(
            provider=context.provider, security_code="000001", provider_document_id=context.provider_document_id,
            artifact_owner_processing_run_id=self.base.fixture.processing_run_id,
        )
        self.record = self.base.paths.data_path(self.record_relpath)
        self.assertEqual(self.record.read_bytes(), self.raw_envelope)

    def _budget(self):
        # Literal physical read inventory for this fixture: readiness manifest,
        # preparation, full tree, provider/snapshot/routes, one API envelope,
        # source, three image payloads, final immutable envelope recheck.
        # This is independent of consumer counters/implementation enumeration.
        reference = self.base.winner.artifact_readiness
        manifest = json.loads(self.base.paths.data_path(Path(reference.manifest_relpath)).read_bytes())
        preparation = json.loads(self.base.paths.data_path(Path(manifest["preparation_relpath"])).read_bytes())
        derived = sum(preparation[key]["byte_count"] for key in
                      ("provider_document_plan", "document_unit_snapshot_plan", "semantic_route_receipts_plan"))
        tree = self.base.fixture.local_materialization_receipt
        byte_count = (reference.manifest_byte_count + manifest["preparation_byte_count"] + tree.output_byte_count
                      + derived + 2 * len(self.raw_envelope) + len(self.base.raw_source)
                      + sum(len(raw) for _, raw, _ in _IMAGES))
        file_count = 2 + tree.output_file_count + 3 + 2 + 1 + 3
        return byte_count, file_count

    def test_three_distinct_image_refs_fit_fixed_budget_in_two_real_reader_pages(self):
        byte_limit, file_limit = self._budget()
        self.assertLess(byte_limit, 200_000)
        opened = []
        read = evidence_api._read_data_bytes

        def observed(paths, relpath, **kwargs):
            opened.append(Path(relpath))
            return read(paths, relpath, **kwargs)

        with patch.object(evidence_api, "_read_data_bytes", observed):
            confirmation, _ = self.base._consumer(maximum_artifact_bytes=byte_limit,
                                                  maximum_artifact_files=file_limit).confirm(self.base.admission)
        receipt = json.loads(self.base.receipts[-1])
        refs = receipt["artifact_closure"]["public_evidence"]
        self.assertEqual(len(refs), 3)
        self.assertEqual({ref["sha256"] for ref in refs}, {_sha(raw) for _, raw, _ in _IMAGES})
        self.assertEqual([item["row_count"] for item in receipt["page_receipts"]], [1, 1, 0])
        self.assertEqual(len(receipt["source_refs"]), 2)
        self.assertTrue(all(len(unit) == 39 for unit in receipt["units"]))
        self.assertEqual(opened.count(self.record_relpath), 1)
        self.assertEqual(len(opened), 4)  # One envelope plus three distinct image payloads.
        self.assertEqual(receipt["artifact_read_budget"]["bytes_reserved"], byte_limit)
        self.assertEqual(receipt["artifact_read_budget"]["files_reserved"], file_limit)
        self.assertEqual((receipt["snapshot"]["read_only"], receipt["snapshot"]["isolation"]), ("on", "repeatable read"))
        self.assertEqual(receipt["snapshot"]["identity"]["current_role"], "disclosure_reader")
        self.assertEqual(self.base.commits, [])
        self.assertTrue(self.base.rollbacks)
        self.assertEqual(confirmation.consumer_check_receipt_sha256, _sha(self.base.receipts[-1]))
        # Old implementation spends two extra whole envelopes for three refs;
        # this exact cap cannot pass by merely caching duplicate image digests.
        for changes in ({"maximum_artifact_bytes": byte_limit - 1}, {"maximum_artifact_files": file_limit - 1}):
            self.base.receipts.clear()
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "artifact read budget"):
                self.base._consumer(**changes).confirm(self.base.admission)
            self.assertEqual(self.base.receipts, [])

    def test_final_envelope_drift_and_payload_error_do_not_persist_confirmation(self):
        actual = consumer_module.read_unit_evidence
        seen = []

        def drift_after_last(**kwargs):
            value = actual(**kwargs)
            seen.append(kwargs["digest"])
            if len(seen) == 3:
                # Same length valid JSON drift after the reused envelope was
                # read; only the final immutable recheck detects this timing.
                old = self.record.read_bytes()
                changed = old.replace(b'"cninfo"', b'"broken"', 1)
                self.assertNotEqual(changed, old)
                self.assertEqual(len(changed), len(old))
                self.record.write_bytes(changed)
            return value

        with patch.object(consumer_module, "read_unit_evidence", drift_after_last):
            with self.assertRaises(AtomicPublicationArtifactConflict):
                self.base._consumer().confirm(self.base.admission)
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.base.receipts, [])
        self.assertTrue(self.base.rollbacks)
        self.record.write_bytes(self.raw_envelope)
        error = OSError("independent image reader failure")

        def fail_payload(**kwargs):
            actual(**kwargs)
            raise error

        with patch.object(consumer_module, "read_unit_evidence", fail_payload):
            with self.assertRaises(OSError) as caught:
                self.base._consumer().confirm(self.base.admission)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.base.receipts, [])
        # A new call must verify afresh and can succeed after exact restoration.
        self.base._consumer().confirm(self.base.admission)
        self.assertEqual(len(self.base.receipts), 1)


if __name__ == "__main__":
    unittest.main()
