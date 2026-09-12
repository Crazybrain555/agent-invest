"""Independent literal H2 runtime data primitives; no IO/process authority fixtures."""

from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.mineru_diagnostic_quality import (
    FRAME_HEADER_BYTES, FRAME_KINDS, FRAME_MAGIC, QUALITY_FILE_SLOTS, QUALITY_ROLES,
    H2ByteBudget, QualityError, QualityFrameHeader, QualityFrameReceipt, QualityStreamReceipt, RetainedFileSeal,
    decode_quality_frame_header, decode_quality_value, encode_quality_frame_header, encode_quality_value,
)


I63 = 2**63 - 1
U64 = 2**64 - 1
REGULAR_PRIVATE = 0o100600
SLOTS = (
    'input.json', 'producer.request.json', 'producer.source.json', 'producer.build.json',
    'producer.reads.json', 'producer.control.raw', 'producer.stderr.raw',
    'verifier.request.json', 'verifier.source.json', 'verifier.build.json',
    'verifier.reads.json', 'verifier.control.raw', 'verifier.stderr.raw',
    'comparison.json', 'qualification.json', 'result.json',
)


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def literals():
    # Complete expected mappings are written here; no to_payload or encoder is
    # used to generate an expected value, and no retained runtime facts are claimed.
    return (
        (H2ByteBudget, {'semantic_record_bytes': 11, 'build_record_bytes': 13, 'comparison_evidence_bytes': 17,
                       'child_control_bytes': 19, 'child_stderr_bytes': 23, 'retained_total_bytes': 29}),
        (QualityFrameHeader, {'kind': 'S', 'payload_bytes': 258}),
        (QualityStreamReceipt, {'observed_bytes': 13, 'retained_bytes': 8, 'discarded_bytes': 5,
                                'eof_observed': True, 'total_bytes': 13}),
        (QualityFrameReceipt, {'header_observed_bytes': 13, 'kind': 'B', 'declared_payload_bytes': 3,
                               'payload_observed_bytes': 3, 'payload_retained_bytes': 3, 'payload_discarded_bytes': 0,
                               'complete': True, 'payload_sha256': sha(b'abc')}),
        (QualityError, {'stage': 'decode', 'exception_type': 'ValueError', 'message': '中',
                       'message_truncated': False, 'original_message_bytes': 3}),
        (RetainedFileSeal, {'slot': 'producer.stderr.raw', 'identity': [17, 23, REGULAR_PRIVATE, 501],
                           'byte_count': 0, 'sha256': sha(b''), 'evidence_kind': 'complete'}),
    )


def construct(record_type, payload):
    arguments = dict(payload)
    if record_type is RetainedFileSeal:
        arguments['identity'] = tuple(arguments['identity'])
    return record_type(**arguments)


def frame(**changes):
    return {'header_observed_bytes': 13, 'kind': 'S', 'declared_payload_bytes': 3,
            'payload_observed_bytes': 3, 'payload_retained_bytes': 3, 'payload_discarded_bytes': 0,
            'complete': True, 'payload_sha256': sha(b'abc'), **changes}


class RuntimeQualityLiteralTests(unittest.TestCase):
    def test_six_literal_mappings_direct_roundtrip_and_exact_canonical_codec_bytes(self):
        for record_type, expected in literals():
            with self.subTest(record=record_type.__name__):
                value = construct(record_type, expected)
                self.assertEqual(value.to_payload(), expected)
                self.assertEqual(record_type.from_payload(expected), value)
                self.assertEqual(record_type.from_payload(value.to_payload()), value)
                raw = canonical(expected)
                self.assertEqual(encode_quality_value(value, maximum_bytes=len(raw)), raw)
                self.assertEqual(decode_quality_value(raw, record_type, maximum_bytes=len(raw)), value)
                self.assertNotIn('contract_version', expected)
                self.assertFalse(hasattr(value, '__dict__'))
                with self.assertRaises((FrozenInstanceError, AttributeError)):
                    setattr(value, next(iter(expected)), None)

    def test_each_primitive_requires_complete_closed_object_fields(self):
        class MappingSubclass(dict):
            pass
        for record_type, expected in literals():
            for wrong in ({**expected, 'verified': True}, {k: v for k, v in expected.items() if k != next(iter(expected))},
                          list(expected.items()), MappingSubclass(expected), None):
                with self.subTest(record=record_type.__name__, shape=type(wrong).__name__), self.assertRaises((ValueError, TypeError)):
                    record_type.from_payload(wrong)

    def test_codec_rejects_bare_dict_subclasses_and_arbitrary_record_types(self):
        for record_type, payload in literals():
            class Subclass(record_type):
                pass
            value = construct(Subclass, {**payload, **({'identity': tuple(payload['identity'])} if record_type is RetainedFileSeal else {})})
            with self.subTest(record=record_type.__name__):
                for wrong in (payload, value):
                    with self.assertRaises((ValueError, TypeError)):
                        encode_quality_value(wrong, maximum_bytes=4096)
                for wrong_type in (dict, object, Subclass, record_type.from_payload(payload)):
                    with self.assertRaises((ValueError, TypeError)):
                        decode_quality_value(canonical(payload), wrong_type, maximum_bytes=4096)

    def test_record_ceiling_rejects_before_any_projection_for_all_six_types(self):
        for record_type, payload in literals():
            value = construct(record_type, payload)
            for limit in (0, 1, True, -1):
                with self.subTest(record=record_type.__name__, limit=limit), patch.object(
                    record_type, 'to_payload', side_effect=AssertionError('projection entered before budget refusal'),
                ) as project:
                    with self.assertRaises((ValueError, TypeError)):
                        encode_quality_value(value, maximum_bytes=limit)
                    project.assert_not_called()
        # The largest legal retained error should not be expanded for a tiny
        # otherwise-valid grant. Generic JSON depth tests live elsewhere.
        error = QualityError('read', 'OSError', 'x' * 2048, False, 2048)
        with patch.object(QualityError, 'to_payload', side_effect=AssertionError('oversized projection')) as project:
            with self.assertRaises((ValueError, TypeError)):
                encode_quality_value(error, maximum_bytes=128)
            project.assert_not_called()

    def test_record_codec_exact_boundaries_and_noncanonical_wire_are_not_silently_repaired(self):
        for record_type, payload in literals():
            raw = canonical(payload)
            value = construct(record_type, payload)
            with self.subTest(record=record_type.__name__):
                with self.assertRaises((ValueError, TypeError)):
                    encode_quality_value(value, maximum_bytes=len(raw) - 1)
                with self.assertRaises((ValueError, TypeError)):
                    decode_quality_value(raw, record_type, maximum_bytes=len(raw) - 1)
                with self.assertRaises((ValueError, TypeError)):
                    decode_quality_value(raw + b'\n', record_type, maximum_bytes=len(raw) + 1)
                with self.assertRaises((ValueError, TypeError)):
                    decode_quality_value(raw, QualityError if record_type is not QualityError else QualityFrameHeader,
                                         maximum_bytes=len(raw))


class RuntimeQualityBudgetHeaderTests(unittest.TestCase):
    def test_all_16_slot_ceilings_and_exact_unprepaid_sum_follow_literal_mapping(self):
        budget = H2ByteBudget(11, 13, 17, 19, 23, 29)
        expected = (17, 19, 11, 13, 17, 19, 23, 19, 11, 13, 17, 19, 23, 17, 17, 17)
        self.assertEqual(QUALITY_ROLES, ('producer', 'verifier'))
        self.assertEqual(QUALITY_FILE_SLOTS, SLOTS)
        self.assertEqual(tuple(budget.slot_limit(slot) for slot in SLOTS), expected)
        self.assertEqual(budget.slot_ceiling_total, 272)
        self.assertEqual(budget.retained_total_bytes, 29)
        for slot in ('producer.stdin.raw', '../input.json', 'producer.source.json.extra', '', None):
            with self.subTest(slot=slot), self.assertRaises((ValueError, TypeError)):
                budget.slot_limit(slot)

    def test_budget_fields_require_positive_exact_ints_and_sum_does_not_overflow(self):
        _, payload = literals()[0]
        for field in payload:
            for invalid in (False, True, 0, -1, 1.0, '1', I63 + 1):
                with self.subTest(field=field, invalid=invalid), self.assertRaises((ValueError, TypeError)):
                    H2ByteBudget(**{**payload, field: invalid})
        maximal = H2ByteBudget(I63, I63, I63, I63, I63, 1)
        self.assertEqual(maximal.slot_ceiling_total, 16 * I63)
        self.assertEqual(maximal.slot_limit('result.json'), I63)

    def test_header_literal_endianness_all_kinds_and_u64_max_without_payload_allocation(self):
        self.assertEqual((FRAME_MAGIC, FRAME_HEADER_BYTES, FRAME_KINDS), (b'M6Q1', 13, ('S', 'B', 'M', 'C')))
        exact = bytes.fromhex('4d365131530000000000000102')
        self.assertEqual(encode_quality_frame_header(QualityFrameHeader('S', 258)), exact)
        self.assertEqual(decode_quality_frame_header(exact), QualityFrameHeader('S', 258))
        for kind in ('S', 'B', 'M', 'C'):
            expected = b'M6Q1' + kind.encode('ascii') + b'\x00' * 8
            with self.subTest(kind=kind):
                self.assertEqual(encode_quality_frame_header(QualityFrameHeader(kind, 0)), expected)
                self.assertEqual(decode_quality_frame_header(expected), QualityFrameHeader(kind, 0))
        maximal = b'M6Q1C' + b'\xff' * 8
        self.assertEqual(encode_quality_frame_header(QualityFrameHeader('C', U64)), maximal)
        self.assertEqual(decode_quality_frame_header(maximal).payload_bytes, U64)

    def test_header_rejects_partial_excess_wrong_magic_kind_and_scalar_coercion(self):
        raw = bytes.fromhex('4d365131530000000000000102')
        for wrong in [raw[:length] for length in range(13)] + [raw + b'x', b'BAD!S' + raw[5:], b'M6Q1X' + raw[5:],
                                                             bytearray(raw), memoryview(raw), raw.decode('latin1')]:
            with self.subTest(raw_type=type(wrong).__name__, length=len(wrong)), self.assertRaises((ValueError, TypeError)):
                decode_quality_frame_header(wrong)
        for kind in ('s', '', 'SS', '中', b'S', None):
            with self.subTest(kind=kind), self.assertRaises((ValueError, TypeError)):
                QualityFrameHeader(kind, 0)
        for count in (False, True, -1, U64 + 1, 1.0, '1'):
            with self.subTest(count=count), self.assertRaises((ValueError, TypeError)):
                QualityFrameHeader('S', count)


class RuntimeQualityReceiptTests(unittest.TestCase):
    def test_stream_complete_partial_empty_and_discarded_receipts_preserve_accounting(self):
        for payload in (
            {'observed_bytes': 0, 'retained_bytes': 0, 'discarded_bytes': 0, 'eof_observed': False, 'total_bytes': None},
            {'observed_bytes': 0, 'retained_bytes': 0, 'discarded_bytes': 0, 'eof_observed': True, 'total_bytes': 0},
            {'observed_bytes': 31, 'retained_bytes': 23, 'discarded_bytes': 8, 'eof_observed': False, 'total_bytes': None},
            {'observed_bytes': I63, 'retained_bytes': 0, 'discarded_bytes': I63, 'eof_observed': True, 'total_bytes': I63},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(QualityStreamReceipt(**payload).to_payload(), payload)
                self.assertEqual(QualityStreamReceipt.from_payload(payload), QualityStreamReceipt(**payload))

    def test_stream_conservation_eof_and_exact_count_types_fail_closed(self):
        base = {'observed_bytes': 5, 'retained_bytes': 3, 'discarded_bytes': 2, 'eof_observed': True, 'total_bytes': 5}
        for changes in ({'observed_bytes': 6, 'total_bytes': 6}, {'retained_bytes': 4}, {'discarded_bytes': 1},
                        {'total_bytes': None}, {'total_bytes': 6}, {'eof_observed': False}, {'observed_bytes': I63 + 1},
                        {'retained_bytes': -1}, {'eof_observed': 1}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                QualityStreamReceipt(**{**base, **changes})
        empty = {'observed_bytes': 0, 'retained_bytes': 0, 'discarded_bytes': 0, 'eof_observed': True, 'total_bytes': 0}
        for field in ('observed_bytes', 'retained_bytes', 'discarded_bytes', 'total_bytes'):
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                QualityStreamReceipt.from_payload({**empty, field: False})

    def test_incomplete_or_invalid_header_never_claims_a_payload_identity(self):
        for header_bytes in (0, 1, 12, 13):
            payload = frame(header_observed_bytes=header_bytes, kind=None, declared_payload_bytes=None,
                            payload_observed_bytes=0, payload_retained_bytes=0, complete=False, payload_sha256=None)
            with self.subTest(header_bytes=header_bytes):
                self.assertEqual(QualityFrameReceipt(**payload).to_payload(), payload)
                for changes in ({'kind': 'S'}, {'declared_payload_bytes': 0}, {'payload_observed_bytes': 1, 'payload_retained_bytes': 1},
                                {'complete': True}, {'payload_sha256': sha(b'')}):
                    with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                        QualityFrameReceipt(**{**payload, **changes})
        with self.assertRaises((ValueError, TypeError)):
            QualityFrameReceipt(**frame(header_observed_bytes=12))

    def test_empty_partial_complete_and_discarded_frames_bind_only_retained_hash_references(self):
        values = (
            frame(kind='C', declared_payload_bytes=0, payload_observed_bytes=0, payload_retained_bytes=0,
                  payload_sha256=sha(b'')),
            frame(declared_payload_bytes=5, complete=False, payload_sha256=None),
            frame(payload_retained_bytes=1, payload_discarded_bytes=2, payload_sha256=None),
            frame(declared_payload_bytes=U64, complete=False, payload_sha256=None),
            frame(declared_payload_bytes=U64, payload_observed_bytes=I63, payload_retained_bytes=I63,
                  complete=False, payload_sha256=None),
        )
        for payload in values:
            with self.subTest(payload=payload):
                self.assertEqual(QualityFrameReceipt.from_payload(payload).to_payload(), payload)
        # A complete zero-length frame is a framing value, not valid semantic JSON.
        self.assertTrue(QualityFrameReceipt.from_payload(values[0]).complete)

    def test_frame_arrival_conservation_completion_and_hash_presence_are_exact(self):
        for changes in ({'header_observed_bytes': -1}, {'header_observed_bytes': 14}, {'kind': 'X'},
                        {'declared_payload_bytes': 2}, {'payload_observed_bytes': 2}, {'payload_retained_bytes': 2},
                        {'payload_discarded_bytes': 1}, {'complete': False}, {'payload_sha256': None},
                        {'payload_sha256': 'sha256:' + 'A' * 64}, {'complete': 1}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                QualityFrameReceipt(**frame(**changes))
        for payload in (frame(declared_payload_bytes=5, complete=False),
                        frame(payload_retained_bytes=1, payload_discarded_bytes=2)):
            with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                QualityFrameReceipt(**payload)
        for observed, retained, discarded in ((I63 + 1, I63, 1), (I63 + 1, I63 + 1, 0), (I63 + 1, 0, I63 + 1)):
            with self.subTest(observed=observed, retained=retained, discarded=discarded), self.assertRaises((ValueError, TypeError)):
                QualityFrameReceipt(**frame(declared_payload_bytes=U64, payload_observed_bytes=observed,
                    payload_retained_bytes=retained, payload_discarded_bytes=discarded, complete=False, payload_sha256=None))
        empty = frame(declared_payload_bytes=0, payload_observed_bytes=0, payload_retained_bytes=0, payload_sha256=sha(b''))
        for field in ('declared_payload_bytes', 'payload_observed_bytes', 'payload_retained_bytes', 'payload_discarded_bytes'):
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                QualityFrameReceipt.from_payload({**empty, field: False})


class RuntimeQualityErrorSealTests(unittest.TestCase):
    def test_error_message_exact_utf8_limit_and_explicit_known_or_unknown_truncation(self):
        for message in ('', '中' * 682 + 'ab'):
            payload = {'stage': 'r' * 64, 'exception_type': 'E' * 128, 'message': message,
                       'message_truncated': False, 'original_message_bytes': len(message.encode())}
            self.assertEqual(QualityError(**payload).to_payload(), payload)
        self.assertEqual(len(('中' * 682 + 'ab').encode()), 2048)
        self.assertEqual(QualityError(' ', '~', '', False, 0).stage, ' ')
        for size in (None, 4, I63):
            value = QualityError('read', 'Error', '中', True, size)
            self.assertEqual(value.original_message_bytes, size)
        with self.assertRaises((ValueError, TypeError)):
            QualityError('read', 'Error', '中' * 683, True, None)

    def test_error_does_not_guess_original_size_truncate_implicitly_or_coerce_types(self):
        _, base = literals()[4]
        for changes in ({'stage': ''}, {'stage': 'x' * 65}, {'stage': '读'}, {'stage': 'read\n'},
                        {'exception_type': 'E' * 129}, {'exception_type': '\x7f'}, {'exception_type': ''},
                        {'message': b'x'}, {'message_truncated': 0}, {'original_message_bytes': None},
                        {'original_message_bytes': 1}, {'original_message_bytes': 4}, {'original_message_bytes': I63 + 1},
                        {'message_truncated': True, 'original_message_bytes': 3}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                QualityError.from_payload({**base, **changes})
        with self.assertRaises((ValueError, TypeError)):
            QualityError('read', 'Error', '', False, False)
        with self.assertRaises((ValueError, TypeError)):
            QualityError('read', 'Error', 'x', True, -1)

    def test_seal_fixed_slots_regular_private_identity_zero_lengths_and_prefixes_are_data(self):
        for kind in ('complete', 'failure_prefix'):
            for slot in SLOTS:
                value = RetainedFileSeal(slot, (0, 1, REGULAR_PRIVATE, 0), 0, sha(b''), kind)
                self.assertEqual(value.to_payload()['identity'], [0, 1, REGULAR_PRIVATE, 0])
                self.assertEqual(RetainedFileSeal.from_payload(value.to_payload()), value)
                self.assertEqual(value.evidence_kind, kind)
        maximal = RetainedFileSeal('producer.source.json', (I63, I63, REGULAR_PRIVATE, I63), I63, sha(b'data reference'), 'failure_prefix')
        self.assertEqual(maximal.byte_count, I63)
        self.assertFalse(hasattr(value, 'verified'))

    def test_seal_rejects_nonregular_nonprivate_foreign_shape_and_noncanonical_hash(self):
        _, base = literals()[5]
        for identity in ([17, 0, REGULAR_PRIVATE, 501], [-1, 23, REGULAR_PRIVATE, 501], [17, 23, 0o600, 501],
                         [17, 23, 0o040600, 501], [17, 23, 0o100644, 501], [17, 23, REGULAR_PRIVATE, -1],
                         [17, 23, REGULAR_PRIVATE], [17, 23, REGULAR_PRIVATE, 501, 1],
                         [False, 23, REGULAR_PRIVATE, 501], [17, True, REGULAR_PRIVATE, 501],
                         [17, 23, REGULAR_PRIVATE, I63 + 1], (17, 23, REGULAR_PRIVATE, 501)):
            with self.subTest(identity=identity), self.assertRaises((ValueError, TypeError)):
                RetainedFileSeal.from_payload({**base, 'identity': identity})
        for changes in ({'slot': '../input.json'}, {'slot': 'producer.source.json.extra'}, {'byte_count': False},
                        {'byte_count': -1}, {'byte_count': I63 + 1}, {'sha256': 'a' * 64}, {'sha256': 'sha256:' + 'A' * 64},
                        {'evidence_kind': 'unused'}, {'evidence_kind': 'verified'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                RetainedFileSeal.from_payload({**base, **changes})
        with self.assertRaises((ValueError, TypeError)):
            RetainedFileSeal(**base)


if __name__ == '__main__':
    unittest.main()
