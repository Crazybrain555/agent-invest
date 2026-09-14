"""Independent contract cases; actual SQL/identity is in the scratch companion."""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from disclosure_anchor.adapters.db.postgres.m6_publish_verifier import PostgresM6PublishVerifier


SOURCE = "sha256:" + "1" * 64
OTHER = "sha256:" + "2" * 64


class _Rows:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self.value

    def scalar_one(self):
        return self.value


class _HistorySession:
    """Only supplies query results: deliberately does not interpret SQL."""

    def __init__(self, bases=(), witnesses=(), unknown=0):
        self.results = iter((list(bases), list(witnesses), unknown))

    def execute(self, *args, **kwargs):
        value = next(self.results)
        if isinstance(value, BaseException):
            raise value
        return _Rows(value)


class _HistoryReader(PostgresM6PublishVerifier):
    def __init__(self, session, sink):
        super().__init__(engine=Mock(), receipt_sink=sink)
        self.session = session
        self.closed = False

    @contextmanager
    def _snapshot(self):
        try:
            yield self.session, {"observed_at": datetime(2026, 1, 1, tzinfo=UTC)}
        finally:
            self.closed = True


def _base(seq, run, pages, **changes):
    return {
        "ledger_seq": seq, "processing_run_id": run, "document_id": "document-" + run,
        "source_identity_sha256": SOURCE, "source_page_count": pages,
        "run_source": SOURCE, "run_document_id": "document-" + run, **changes,
    }


def _witness(kind, **changes):
    return {
        "processing_run_id": "old-without-base", "document_id": "legacy-document",
        "run_source": SOURCE, "document_source": SOURCE, "payload_source": None,
        "witness_kind": kind, **changes,
    }


class M6PrivatePublicationContractTests(unittest.TestCase):
    def test_invalid_scope_and_non_e2e_admission_do_not_open_connection(self):
        engine, sink = Mock(), Mock()
        reader = PostgresM6PublishVerifier(engine=engine, receipt_sink=sink)
        invalid = (None, [], (), (SOURCE, SOURCE), (OTHER, SOURCE), ("bad",),
                   ("sha256:" + "A" * 64,), tuple(f"sha256:{i:064x}" for i in range(10001)))
        for sources in invalid:
            with self.subTest(scope_type=type(sources).__name__, length=len(sources or ())):
                with self.assertRaises(ValueError):
                    reader.first_ledger_for(sources)
        with self.assertRaises(ValueError):
            reader.read_publication(None)
        engine.connect.assert_not_called()
        sink.assert_not_called()

    def test_first_global_ledger_and_page_variants_have_exact_persisted_receipt(self):
        # Oldest run belongs to a different document, before the current campaign.
        rows = (_base(7, "before-campaign", 34), _base(81, "current", 158),
                _base(90, "later", 158))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-receipt.json"
            reader = _HistoryReader(_HistorySession(rows), path.write_bytes)
            fact, empty = reader.first_ledger_for((SOURCE, OTHER))
            self.assertEqual((fact.first_processing_run_id, fact.first_ledger_seq,
                              fact.first_source_page_count, fact.source_page_variants),
                             ("before-campaign", 7, 34, 2))
            self.assertTrue(fact.scan_complete)
            self.assertTrue(empty.scan_complete)
            self.assertIsNone(empty.first_ledger_seq)
            self.assertEqual(empty.source_page_variants, 0)
            raw = path.read_bytes()
            self.assertEqual(fact.audit_receipt_sha256, "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertEqual(empty.audit_receipt_sha256, fact.audit_receipt_sha256)
            value = json.loads(raw)
            self.assertEqual(raw, json.dumps(value, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":"), allow_nan=False).encode())
            self.assertEqual(value["projections"][0]["first_ledger_seq"], 7)
            self.assertTrue(reader.closed)

    def test_publication_witness_without_base_never_becomes_fresh(self):
        for witness in (_witness("run"), _witness("outbox"), _witness("published_document"),
                        _witness("run", run_source=None)):
            with self.subTest(witness=witness):
                reader = _HistoryReader(_HistorySession(witnesses=(witness,)), lambda raw: None)
                fact, = reader.first_ledger_for((SOURCE,))
                self.assertFalse(fact.scan_complete)
                self.assertIsNone(fact.first_ledger_seq)
                self.assertEqual(fact.source_page_variants, 0)

    def test_incomplete_history_preserves_known_first_without_inventing_provenance(self):
        cases = (
            _HistorySession((_base(7, "known", 34),), (_witness("outbox"),)),
            _HistorySession((_base(7, "known", 34, run_source=OTHER),)),
            _HistorySession((_base(7, "known", 34, run_document_id="wrong-owner"),)),
            _HistorySession((_base(7, "known", 34),), (
                _witness("outbox", processing_run_id="known", document_id="other-document"),
            )),
            _HistorySession((_base(7, "known", 34),), unknown=1),
        )
        for session in cases:
            with self.subTest(session=session):
                fact, = _HistoryReader(session, lambda raw: None).first_ledger_for((SOURCE,))
                self.assertFalse(fact.scan_complete)
                self.assertEqual((fact.first_ledger_seq, fact.first_source_page_count), (7, 34))

    def test_receipt_persistence_failure_propagates_even_after_partial_write(self):
        failure = OSError("private evidence disk unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial-receipt"

            def failing_sink(raw):
                path.write_bytes(raw[:31])
                raise failure

            reader = _HistoryReader(_HistorySession(), failing_sink)
            with self.assertRaises(OSError) as caught:
                reader.first_ledger_for((SOURCE,))
            self.assertIs(caught.exception, failure)
            self.assertEqual(path.stat().st_size, 31)
            self.assertTrue(reader.closed)

    def test_unexpected_query_errors_and_cancellation_are_not_empty_history(self):
        for error in (RuntimeError("database protocol drift"), KeyboardInterrupt("cancel")):
            with self.subTest(error=type(error).__name__):
                session = _HistorySession()
                session.results = iter((error,))
                sink = Mock()
                reader = _HistoryReader(session, sink)
                with self.assertRaises(type(error)) as caught:
                    reader.first_ledger_for((SOURCE,))
                self.assertIs(caught.exception, error)
                self.assertTrue(reader.closed)
                sink.assert_not_called()


if __name__ == "__main__":
    unittest.main()
