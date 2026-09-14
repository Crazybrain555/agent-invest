"""Independent input-boundary tests; real public SQL/files are tested in scratch."""

from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import Mock

from disclosure_anchor.adapters.runtime.m6_public_consumer_verifier import (
    M6PublicAuditInput, M6PublicConsumerVerifier,
)
from disclosure_anchor.application.contracts.m6_run import M6SourceHistoryFact
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted, M6PublicationCommitted
import tests.unit.test_atomic_document_publication_v4 as atomic_fixture


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


class M6PublicConsumerInputTests(unittest.TestCase):
    def setUp(self):
        # Existing sealed contracts supply identities. This input fixture is
        # used only for failures before artifact/public I/O; it is not a fake
        # positive publication or an authenticated database observation.
        request = atomic_fixture._request()
        winner = atomic_fixture._winner(request)
        self.admission = M6AttemptAdmitted(
            attempt_id=winner.attempt_id, fence_identity=winner.fence_identity,
            document_id=winner.document_id, processing_run_id=winner.processing_run_id,
            source_pdf_sha256=request.upstream_evidence.source_pdf_sha256,
            source_byte_count=100, source_page_count=2,
            process_profile_sha256=request.upstream_evidence.process_profile_sha256,
        )
        publication = M6PublicationCommitted(
            attempt_id=winner.attempt_id, processing_run_id=winner.processing_run_id,
            document_id=winner.document_id, source_pdf_sha256=self.admission.source_pdf_sha256,
            source_page_count=2, ledger_seq=7, winner_sha256=winner.sha256,
            durable_base_sha256=winner.durable_base_commit.durable_base_sha256,
        )
        snapshot = {"read_only": "on", "isolation": "repeatable read", "snapshot": "10:10:",
                    "observed_at": "2026-01-01T00:00:00+00:00", "identity": {
                        "database_name": "invest_engine", "session_role": "disclosure_app",
                        "current_role": "disclosure_app", "session_superuser": False, "current_superuser": False}}
        self.publication_row = {"contract_version": "m6.private-publication-audit.v1", "snapshot": snapshot,
                                "admission": self.admission.model_dump(mode="json"),
                                "checkpoint_sha256": "sha256:" + "a" * 64,
                                "publication": publication.model_dump(mode="json"),
                                "winner_canonical_json": winner.canonical_bytes.decode(),
                                "published_event_id": winner.outbox_commit.processing_run_published_event_id}
        projection = dict(source_pdf_sha256=self.admission.source_pdf_sha256, scan_complete=True,
                          first_processing_run_id=self.admission.processing_run_id, first_ledger_seq=7,
                          first_source_page_count=2, source_page_variants=1)
        self.history_row = {"contract_version": "m6.private-source-history-audit.v1", "snapshot": snapshot,
                            "sources": [self.admission.source_pdf_sha256], "base_rows": [],
                            "publication_witnesses": [], "unattributed_publication_count": 0,
                            "projections": [projection | {"coverage_gaps": [], "base_identity_conflicts": []}],
                            "query_sha256": "sha256:" + "b" * 64}
        publication_raw, history_raw = _canonical(self.publication_row), _canonical(self.history_row)
        history = M6SourceHistoryFact(**projection, audit_receipt_sha256=_sha(history_raw))
        self.audit = M6PublicAuditInput(publication=publication, publication_audit_sha256=_sha(publication_raw),
                                      publication_audit=publication_raw, history=history, history_audit=history_raw)
        self.engine, self.paths, self.sink = Mock(), Mock(), Mock()

    def _consumer(self, **changes):
        arguments = dict(engine=self.engine, paths=self.paths, private_audit_for=lambda admission: self.audit,
                         receipt_sink=self.sink, verifier_identity="independent-verifier")
        return M6PublicConsumerVerifier(**(arguments | changes))

    def _assert_no_external_read_or_success(self):
        self.engine.connect.assert_not_called()
        self.paths.data_path.assert_not_called()
        self.sink.assert_not_called()

    def test_invalid_configuration_and_non_e2e_admission_are_rejected_before_io(self):
        for change in ({"page_size": 0}, {"page_size": 501}, {"page_size": True},
                       {"maximum_receipt_bytes": 0}, {"maximum_receipt_bytes": 64 * 1024**2 + 1},
                       {"maximum_receipt_bytes": True}, {"verifier_identity": ""},
                       {"maximum_artifact_bytes": 0}, {"maximum_artifact_files": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self._consumer(**change)
        loader = Mock()
        for admission in (None, self.admission.model_copy(update={"document_id": None})):
            with self.subTest(admission=admission), self.assertRaises(ValueError):
                self._consumer(private_audit_for=loader).confirm(admission)
        loader.assert_not_called()
        self._assert_no_external_read_or_success()

    def test_audit_loader_failure_and_cancellation_propagate_without_writer_fallback(self):
        for failure in (OSError("frozen audit unavailable"), KeyboardInterrupt("cancel audit read")):
            with self.subTest(failure=type(failure).__name__):
                loader = Mock(side_effect=failure)
                with self.assertRaises(type(failure)) as caught:
                    self._consumer(private_audit_for=loader).confirm(self.admission)
                self.assertIs(caught.exception, failure)
                loader.assert_called_once_with(self.admission)
        self._assert_no_external_read_or_success()

    def test_raw_audit_hash_canonical_bytes_and_size_are_not_optional(self):
        raw_cases = (b"{}", b"", self.audit.publication_audit + b"\n",
                     self.audit.publication_audit[:-1] + b',"contract_version":"duplicate"}',
                     b"x" * (16 * 1024**2 + 1))
        for raw in raw_cases:
            # Both an unchanged pin (byte drift) and a recomputed digest
            # (malformed/noncanonical contract) must reject before any IO.
            for digest in (self.audit.publication_audit_sha256, _sha(raw)):
                with self.subTest(byte_count=len(raw), digest_matches=digest == _sha(raw)):
                    altered = replace(self.audit, publication_audit=raw, publication_audit_sha256=digest)
                    with self.assertRaises(ValueError):
                        self._consumer(private_audit_for=lambda admission: altered).confirm(self.admission)
        self._assert_no_external_read_or_success()

    def test_private_audit_closed_contract_and_principal_are_verified_from_bytes(self):
        cases = []
        extra = json.loads(self.audit.publication_audit)
        extra["trust_me"] = True
        cases.append(extra)
        for field, value in (("current_role", "disclosure_reader"), ("session_role", "admin"),
                             ("session_superuser", True)):
            changed = json.loads(self.audit.publication_audit)
            changed["snapshot"]["identity"][field] = value
            cases.append(changed)
        for field, value in (("read_only", "off"), ("isolation", "read committed")):
            changed = json.loads(self.audit.publication_audit)
            changed["snapshot"][field] = value
            cases.append(changed)
        for changed in cases:
            with self.subTest(changed=changed["snapshot"]):
                raw = _canonical(changed)
                altered = replace(self.audit, publication_audit=raw, publication_audit_sha256=_sha(raw))
                with self.assertRaisesRegex(ValueError, "fields|principal|snapshot"):
                    self._consumer(private_audit_for=lambda admission: altered).confirm(self.admission)
        self._assert_no_external_read_or_success()

    def test_cross_admission_and_history_projection_cannot_borrow_another_receipt(self):
        for change in ({"fence_identity": "other-fence"}, {"source_byte_count": 101},
                       {"source_page_count": 3}, {"process_profile_sha256": _sha(b"independent other process profile")}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "admission"):
                self._consumer().confirm(self.admission.model_copy(update=change))
        for change in ({"first_ledger_seq": 8}, {"scan_complete": False}, {"source_page_variants": 2}):
            altered = replace(self.audit, history=self.audit.history.model_copy(update=change))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "history projection"):
                self._consumer(private_audit_for=lambda admission: altered).confirm(self.admission)
        # Python equality considers True == 1; a raw private JSON integer must
        # still not satisfy the explicitly boolean scan_complete contract.
        changed = json.loads(self.audit.history_audit)
        changed["projections"][0]["scan_complete"] = 1
        raw = _canonical(changed)
        altered = replace(self.audit, history_audit=raw,
                          history=self.audit.history.model_copy(update={"audit_receipt_sha256": _sha(raw)}))
        with self.assertRaisesRegex(ValueError, "boolean"):
            self._consumer(private_audit_for=lambda admission: altered).confirm(self.admission)
        self._assert_no_external_read_or_success()


if __name__ == "__main__":
    unittest.main()
