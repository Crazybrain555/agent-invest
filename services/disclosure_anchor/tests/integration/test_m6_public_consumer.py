"""Actual reader-principal/public-view and artifact checks in managed scratch."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.schema import READER_ROLE
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import AtomicPublicationArtifactConflict
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    seal_atomic_publication_request_v4, seal_pre_id_unit_publication_v4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    semantic_adjudication_terminal_v1, semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import ConfigurationError, ParserOutputContractError
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes, content_hash_aggregate, structure_hash_aggregate,
)
import tests.integration._remote_parse_v4_factory as authority_factory
import tests.integration.test_atomic_document_publisher_v4 as atomic_fixture
import tests.integration.test_m6_private_publication as private_fixture


class _ConsumerPaths(atomic_fixture._Paths):
    # Reuse the real API path constructor with the fixture's existing data root.
    provider_document_relpath = FileStorePathBuilder.provider_document_relpath


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _two_unit_request(original):
    """A second explicit source-page Unit; reseal the original public contracts."""
    first = original.units[0]
    values = asdict(first)
    values.pop("routed_draft_sha256")
    # Opaque public JSON must preserve JSON number vs boolean identity too.
    payload = {"text": "Second independent public Unit", "fixture_numeric_value": 1}
    locator = json.loads(first.canonical_artifact_locator_json)
    locator["unit_index"] = 1
    locator_raw = _canonical(locator)
    hashes = compute_unit_hashes(payload_kind="text", payload=payload, title=None,
                                 heading_path=[], semantic_keys=None, section_keys=None,
                                 quality_status="ok", applicability="applicable", order_index=2)
    values.update(unit_index=2, title=None, page_no=2, page_numbers=(2,), section_keys=None,
                  canonical_payload_json=_canonical(payload).decode(), content_hash=hashes.content_hash,
                  structure_hash=hashes.structure_hash, query_projection_hash=hashes.query_projection_hash,
                  canonical_artifact_locator_json=locator_raw.decode(), provider_locator_sha256=_sha(locator_raw))
    second = seal_pre_id_unit_publication_v4(**values)
    second_route = replace(original.semantic_route_receipts[0], unit_order_index=2,
                           provider_locator_sha256=second.provider_locator_sha256,
                           routed_draft_sha256=second.routed_draft_sha256)
    routes = (original.semantic_route_receipts[0], second_route)
    terminal = semantic_adjudication_terminal_v1(tuple(row.receipt for row in routes))
    projection = json.loads(original.processing_run_projection_json)
    projection.update(unit_count=2, content_hash_aggregate=content_hash_aggregate((first.content_hash, second.content_hash)),
                      structure_hash_aggregate=structure_hash_aggregate((first.structure_hash, second.structure_hash)),
                      semantic_route_receipts_sha256=_sha(semantic_route_receipts_file_bytes_v3(routes)),
                      semantic_adjudication_status=terminal.status, semantic_adjudication_summary=terminal.summary,
                      semantic_degraded_unit_count=terminal.degraded_unit_count,
                      semantic_failover_group_count=terminal.failover_group_count)
    raw = _canonical(projection)
    return seal_atomic_publication_request_v4(
        identity=original.identity, upstream_evidence=original.upstream_evidence,
        source_page_count=original.source_page_count, processing_run_projection_json=raw.decode(),
        processing_run_projection_sha256=_sha(raw),
        semantic_route_receipts_contract_version=original.semantic_route_receipts_contract_version,
        semantic_route_receipts=routes,
        expected_unit_build_status_before=original.expected_unit_build_status_before,
        expected_unit_build_attempt_count_before=original.expected_unit_build_attempt_count_before,
        previous_active_units=original.previous_active_units,
        previous_active_units_sha256=original.previous_active_units_sha256,
        units=(first, second), contract_version=original.contract_version,
    )


class M6PublicConsumerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from disclosure_anchor.adapters.runtime.m6_public_consumer_verifier import (
            M6PublicAuditInput, M6PublicConsumerVerifier,
        )
        self.consumer_type = M6PublicConsumerVerifier
        self.raw_source = b"Synthetic source bytes for the public consumer; no PDF parser is invoked.".ljust(100, b" ")
        self.assertEqual(len(self.raw_source), 100)
        attempt = "rpa_" + ids.new_ulid()
        original_hash = authority_factory.sha256_bytes
        original_build = authority_factory.build_v4_authority_fixture

        def fixture_hash(raw):
            # The old fixture fixes source_byte_count=100 but hashes an ID
            # string. Only select its initial source value differently; every
            # subsequent contract hash and the consumer use real bytes/hash.
            return _sha(self.raw_source) if raw == (attempt + ":source").encode() else original_hash(raw)

        self.private = private_fixture.M6PrivatePublicationIntegrationTests()
        with patch.object(authority_factory, "sha256_bytes", fixture_hash), patch.object(
            atomic_fixture, "build_v4_authority_fixture", lambda: original_build(attempt_id=attempt),
        ):
            self.private.setUp()
        self.addCleanup(self.private.doCleanups)
        self.atomic, self.fixture = self.private.atomic, self.private.fixture
        self.engine, self.admission = self.private.engine, self.private.admission
        self._bind_security()
        self.atomic.request = _two_unit_request(self.atomic.request)
        self.paths = _ConsumerPaths(self.atomic.root)
        self.raw_path = self.paths.data_path(Path(self.fixture.materialization_intent.provider_envelope_context.source_pdf_relpath))
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_path.write_bytes(self.raw_source)
        self.winner = self.private._publish()
        publication = self.private.reader.read_publication(self.admission)
        publication_raw = self.private.receipts[-1]
        history, = self.private.reader.first_ledger_for((self.admission.source_pdf_sha256,))
        self.audit = M6PublicAuditInput(publication=publication.publication,
                                      publication_audit_sha256=publication.audit_receipt_sha256,
                                      publication_audit=publication_raw, history=history,
                                      history_audit=self.private.receipts[-1])
        self.reader_engine = sa.create_engine(self.engine.url, poolclass=NullPool)
        self.addCleanup(self.reader_engine.dispose)

        @sa.event.listens_for(self.reader_engine, "connect")
        def reader_session(dbapi, _record):
            prior = dbapi.autocommit
            dbapi.autocommit = True
            try:
                with dbapi.cursor() as cursor:
                    cursor.execute("SET SESSION AUTHORIZATION " + READER_ROLE)
            finally:
                dbapi.autocommit = prior

        self.sql, self.rollbacks, self.commits, self.receipts = [], [], [], []
        sa.event.listen(self.reader_engine, "before_cursor_execute", self._observe)
        sa.event.listen(self.reader_engine, "rollback", lambda conn: self.rollbacks.append(True))
        sa.event.listen(self.reader_engine, "commit", lambda conn: self.commits.append(True))

    def _bind_security(self):
        with self.engine.begin() as conn:
            existing = conn.execute(sa.select(models.Security.security_id, models.Security.company_id).where(
                models.Security.security_code == "000001", models.Security.exchange == "SZSE",
            )).one_or_none()
            if existing is None:
                company, security = str(ids.new_company_id()), str(ids.new_security_id())
                conn.execute(sa.insert(models.Company).values(company_id=company, legal_name="Synthetic public verifier fixture"))
                conn.execute(sa.insert(models.Security).values(security_id=security, company_id=company,
                                                               security_code="000001", exchange="SZSE"))
                self.addCleanup(self._clean_security, security, company)
            else:
                security, company = existing
            conn.execute(sa.update(models.Document).where(models.Document.document_id == self.fixture.document_id).values(
                company_id=company, security_id=security,
            ))

    def _clean_security(self, security, company):
        with self.engine.begin() as conn:
            conn.execute(sa.update(models.Document).where(models.Document.document_id == self.fixture.document_id).values(
                company_id=None, security_id=None,
            ))
            conn.execute(sa.delete(models.Security).where(models.Security.security_id == security))
            conn.execute(sa.delete(models.Company).where(models.Company.company_id == company))

    def _observe(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append(" ".join(statement.lower().split()))

    def _consumer(self, **changes):
        values = dict(engine=self.reader_engine, paths=self.paths, private_audit_for=lambda admission: self.audit,
                      receipt_sink=self.receipts.append, verifier_identity="independent-public-consumer", page_size=1)
        return self.consumer_type(**(values | changes))

    def test_real_reader_multi_page_and_documented_route_projection_close_all_artifacts(self):
        with patch.object(FilesystemAtomicPublicationArtifactReadinessV4, "prepare_or_replay",
                          side_effect=AssertionError("consumer attempted preparation")), patch.object(
            ImmutableArtifactStore, "create_or_verify", side_effect=AssertionError("consumer attempted storage write"),
        ):
            confirmation, history = self._consumer().confirm(self.admission)
        self.assertEqual(history, self.audit.history)
        self.assertEqual((confirmation.attempt_id, confirmation.processing_run_id, confirmation.source_pdf_sha256),
                         (self.admission.attempt_id, self.admission.processing_run_id, _sha(self.raw_source)))
        self.assertEqual(confirmation.winner_sha256, self.winner.sha256)
        self.assertEqual(confirmation.history_audit_receipt_sha256, history.audit_receipt_sha256)
        self.assertEqual(confirmation.consumer_check_receipt_sha256, _sha(self.receipts[-1]))
        receipt = json.loads(self.receipts[-1])
        self.assertEqual(receipt["queries_including_empty_tail"], 3)
        self.assertEqual([page["row_count"] for page in receipt["page_receipts"]], [1, 1, 0])
        self.assertEqual([row["asset_id"] for row in receipt["units"]], [row.asset_id for row in self.winner.unit_assets])
        self.assertEqual([len(row) for row in receipt["units"]], [39, 39])
        self.assertEqual([row["section_keys"] for row in receipt["units"]], [["section"], []])
        self.assertEqual([row["semantic_keys"] for row in receipt["units"]], [[], []])
        self.assertEqual(confirmation.public_units_sha256, _sha(_canonical(receipt["units"])))
        self.assertEqual(confirmation.artifact_closure_sha256, _sha(_canonical(receipt["artifact_closure"])))
        self.assertEqual({row["asset_id"] for row in receipt["source_refs"]}, {row.asset_id for row in self.winner.unit_assets})
        self.assertEqual((receipt["snapshot"]["read_only"], receipt["snapshot"]["isolation"]), ("on", "repeatable read"))
        self.assertEqual((receipt["snapshot"]["identity"]["session_role"], receipt["snapshot"]["identity"]["current_role"]),
                         (READER_ROLE, READER_ROLE))
        self.assertGreaterEqual(sum("from disclosure_public.document_units_v1" in sql for sql in self.sql), 2)
        self.assertTrue(any("disclosure_public.source_refs_v1" in sql for sql in self.sql))
        observed = "\n".join(self.sql)
        for forbidden in ("disclosure_core.", "disclosure_ops.", "for update", "for share", "pg_advisory", "token"):
            self.assertNotIn(forbidden, observed)
        self.assertEqual(self.commits, [])
        self.assertTrue(self.rollbacks)
        with self.assertRaises(ConfigurationError):
            self._consumer(engine=self.private.app_engine).confirm(self.admission)

    def test_public_projection_changes_and_missing_last_unit_are_not_confirmation(self):
        unit_table = models.DocumentUnit.__table__
        second = unit_table.c.asset_id == self.winner.unit_assets[1].asset_id
        for field, changed in (("title", "drifted public title"), ("section_keys", ["unexpected_route"]),
                               ("payload", {"text": "Second independent public Unit", "fixture_numeric_value": True})):
            with self.subTest(field=field):
                with self.engine.begin() as conn:
                    original = conn.execute(sa.select(unit_table.c[field]).where(second)).scalar_one()
                    conn.execute(unit_table.update().where(second).values({field: changed}))
                try:
                    with self.assertRaises(ValueError):
                        self._consumer().confirm(self.admission)
                finally:
                    with self.engine.begin() as conn:
                        conn.execute(unit_table.update().where(second).values({field: original}))
        # An additional earlier key must be visible on the first page. Merely
        # moving an expected row negative would also fail an old incomplete
        # scanner, so insert an actual extra row outside the sealed inventory.
        extra_asset = str(ids.new_asset_id())
        with self.engine.begin() as conn:
            extra = dict(conn.execute(sa.select(unit_table).where(second)).mappings().one())
            extra.update(asset_id=extra_asset, order_index=-1)
            conn.execute(unit_table.insert().values(extra))
        try:
            with self.assertRaises(ValueError):
                self._consumer().confirm(self.admission)
        finally:
            with self.engine.begin() as conn:
                conn.execute(unit_table.delete().where(unit_table.c.asset_id == extra_asset))
        with self.engine.begin() as conn:
            conn.execute(unit_table.delete().where(second))
        with self.assertRaises(ValueError):
            self._consumer().confirm(self.admission)
        self.assertEqual(self.receipts, [])

    def test_paginated_reader_keeps_one_snapshot_then_detects_new_drift(self):
        entered, release = Event(), Event()
        intercepted = False

        def pause(conn, cursor, statement, parameters, context, executemany):
            nonlocal intercepted
            if not intercepted and "from disclosure_public.document_units_v1" in statement.lower():
                intercepted = True
                entered.set()
                if not release.wait(10):
                    raise AssertionError("public snapshot barrier was not released")

        sa.event.listen(self.reader_engine, "after_cursor_execute", pause)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._consumer().confirm, self.admission)
            try:
                self.assertTrue(entered.wait(10))
                with self.engine.begin() as conn:
                    conn.execute(sa.update(models.DocumentUnit).where(
                        models.DocumentUnit.asset_id == self.winner.unit_assets[1].asset_id,
                    ).values(title="changed after first public page"))
            finally:
                release.set()
            confirmation, _ = future.result(timeout=10)
        self.assertEqual(confirmation.winner_sha256, self.winner.sha256)
        with self.assertRaises(ValueError):
            self._consumer().confirm(self.admission)
        self.assertEqual(len(self.receipts), 1)

    def test_source_ref_read_failure_and_superseded_run_are_visible(self):
        failure = RuntimeError("source-ref read unavailable")

        def fail_source_refs(conn, cursor, statement, parameters, context, executemany):
            if "disclosure_public.source_refs_v1" in statement.lower():
                raise failure

        sa.event.listen(self.reader_engine, "before_cursor_execute", fail_source_refs)
        try:
            with self.assertRaises(RuntimeError) as caught:
                self._consumer().confirm(self.admission)
            self.assertIs(caught.exception, failure)
        finally:
            sa.event.remove(self.reader_engine, "before_cursor_execute", fail_source_refs)
        with self.engine.begin() as conn:
            conn.execute(sa.update(models.ProcessingRun).where(
                models.ProcessingRun.processing_run_id == self.fixture.processing_run_id,
            ).values(is_active=False))
        with self.assertRaises(ValueError):
            self._consumer().confirm(self.admission)
        self.assertEqual(self.receipts, [])

    def test_exact_private_audits_cannot_be_replaced_by_matching_projection_objects(self):
        for changed in (
            replace(self.audit, publication_audit=b"{}"),
            replace(self.audit, history_audit=b"{}"),
            replace(self.audit, publication=self.audit.publication.model_copy(update={"ledger_seq": self.audit.publication.ledger_seq + 1})),
        ):
            with self.subTest(changed=changed.publication.ledger_seq):
                with self.assertRaises(ValueError):
                    self._consumer(private_audit_for=lambda admission: changed).confirm(self.admission)
        self.assertEqual(self.receipts, [])

    def test_raw_and_immutable_artifact_drift_then_sink_failure_never_return_success(self):
        projection = json.loads(self.atomic.request.processing_run_projection_json)
        files = [self.raw_path] + [self.paths.data_path(Path(projection[key])) for key in (
            "provider_document_relpath", "document_units_relpath", "semantic_route_receipts_relpath",
        )]
        for path in files:
            with self.subTest(artifact=path.name):
                original, mode = path.read_bytes(), path.stat().st_mode & 0o777
                path.chmod(0o600)
                path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                try:
                    with self.assertRaises((ValueError, AtomicPublicationArtifactConflict)):
                        self._consumer().confirm(self.admission)
                finally:
                    path.write_bytes(original)
                    path.chmod(mode)
        extra = self.paths.data_path(Path(self.atomic.request.upstream_evidence.parser_artifact_root_relpath) / "unexpected.fixture")
        extra.write_bytes(b"unexpected parser member")
        try:
            with self.assertRaises(ParserOutputContractError):
                self._consumer().confirm(self.admission)
        finally:
            extra.unlink()
        for limit in ({"maximum_receipt_bytes": 1}, {"maximum_artifact_bytes": 1}, {"maximum_artifact_files": 1}):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "bound|budget"):
                self._consumer(**limit).confirm(self.admission)
        failure = OSError("consumer receipt cannot be persisted")

        def fail_sink(raw):
            raise failure

        with self.assertRaises(OSError) as caught:
            self._consumer(receipt_sink=fail_sink).confirm(self.admission)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.commits, [])


if __name__ == "__main__":
    unittest.main()
