"""Independent Python receipt transport checks; no claim of native persistence."""
import hashlib
import json
import unittest

from disclosure_anchor.adapters.runtime.m6_owner_protocol import M6OwnerProtocolError
from disclosure_anchor.application.contracts.m6_owner import M6DepositReceipt, M6OwnerRequest
from tests import m6_owner_support as support
from tests import m6_support as m6


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(text):
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


class OwnerDepositIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = m6.make_fixture('e2e_publication', {'a': (7, 'fresh')})
        self.spec = self.fixture.spec
        self.anchor = support.anchor_for(self.spec)
        self.clock = support.ManualClock(support.NS)
        self.owner = support.ScriptedOwner(self.spec, self.anchor, self.clock)

    def receipt(self, **updates):
        return canonical({
            'contract_version': 'm6.admission-reconciliation.v1', 'run_id': self.spec.run_id,
            'spec_sha256': self.spec.canonical_sha256(), 'runner_epoch_sha256': m6.RUNNER_EPOCH,
            'last_producer_sequence': 0, 'admitted_attempt_count': 0,
            'admitted_attempt_set_sha256': m6.digest('empty-admission-set'),
            'unresolved_claim_count': 0, 'unresolved_receipt_sha256': None, **updates,
        })

    def command(self, raw=None):
        raw = self.receipt() if raw is None else raw
        return M6DepositReceipt(receipt_kind='admission_reconciliation', receipt_sha256=digest(raw), receipt_utf8=raw)

    def test_command_request_exact_roundtrip_and_complete_envelope(self):
        command = self.command()
        request = M6OwnerRequest(run_id=self.spec.run_id, spec_sha256=self.spec.canonical_sha256(),
                                 request_id='deposit-1', command=command)
        raw = request.canonical_bytes()
        self.assertEqual(M6OwnerRequest.from_canonical_bytes(raw, maximum_bytes=65536), request)
        self.assertGreater(len(raw), len(command.receipt_utf8.encode()))
        self.assertEqual(json.loads(raw)['command']['receipt_sha256'], digest(command.receipt_utf8))

    def test_hash_noncanonical_duplicate_keys_and_wrong_kind_fail(self):
        raw = self.receipt()
        bad = [raw+'\n', json.dumps(json.loads(raw), indent=1), '[]',
               raw[:-1]+',"run_id":"duplicate"}', self.receipt(contract_version='m6.ownership-closure.v1'),
               self.receipt(note=0).replace('"note":0', '"note":-0'),
               self.receipt(note=[{'value': 1.5}])]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.command(value)
        with self.assertRaises(ValueError):
            M6DepositReceipt(receipt_kind='admission_reconciliation', receipt_sha256=m6.digest('wrong'), receipt_utf8=raw)

    def test_run_or_spec_drift_is_rejected_before_transport(self):
        client = support.client_for(self.owner)
        for update in ({'run_id':'other-run'}, {'spec_sha256':m6.digest('other-spec')}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                client.deposit('admission_reconciliation', self.receipt(**update))
        self.assertEqual(self.owner.requests, [])

    def test_controller_cannot_deposit_but_runner_and_verifiers_can(self):
        controller = support.client_for(self.owner, role='controller', epoch=m6.digest('controller'))
        with self.assertRaises(M6OwnerProtocolError):
            controller.deposit('admission_reconciliation', self.receipt())
        self.assertEqual(self.owner.requests, [])
        for role in ('e2e_runner','public_verifier','quality_verifier'):
            with self.subTest(role=role):
                client = support.client_for(self.owner, role=role)
                self.owner.answer(self.owner.status(observed=self.fixture.at(10), state='open'))
                reply = client.deposit('admission_reconciliation', self.receipt())
                self.assertEqual(reply.outcome, 'ok')
                self.assertIsNone(reply.record)
        self.assertEqual(len(self.owner.requests), 3)

    def test_utf8_bound_is_bytes_and_envelope_escaping_still_enforces_wire_bound(self):
        padded = self.receipt(note='')
        exact = self.receipt(note='a'*(49152-len(padded.encode())))
        self.assertEqual(len(exact.encode()), 49152)
        self.command(exact)
        with self.assertRaises(ValueError):
            self.command(self.receipt(note='a'*(49153-len(padded.encode()))))
        unicode_large = self.receipt(note='界'*17000)
        self.assertLess(len(unicode_large), 49152)
        self.assertGreater(len(unicode_large.encode()), 49152)
        with self.assertRaises(ValueError):
            self.command(unicode_large)
        # The inner canonical JSON is small enough, but escaped backslashes in
        # the wire envelope make it exceed64KiB. No transport call is allowed.
        escaped = self.receipt(note='\\'*22000)
        self.assertLess(len(escaped.encode()), 49152)
        client = support.client_for(self.owner)
        with self.assertRaises(M6OwnerProtocolError):
            client.deposit('admission_reconciliation', escaped)
        self.assertEqual(self.owner.requests, [])

    def test_identical_receipt_retries_keep_exact_payload_and_hash_but_new_request_id(self):
        client = support.client_for(self.owner)
        for _ in range(2):
            self.owner.answer(self.owner.status(observed=self.fixture.at(10), state='open'))
            client.deposit('admission_reconciliation', self.receipt())
        first, second = self.owner.requests
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertEqual(first.command.canonical_bytes(), second.command.canonical_bytes())
        self.assertEqual(first.command.receipt_sha256, digest(self.receipt()))


if __name__ == '__main__':
    unittest.main()
