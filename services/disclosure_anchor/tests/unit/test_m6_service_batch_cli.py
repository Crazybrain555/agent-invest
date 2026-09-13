"""Independent local CLI boundary tests; runtime/clock doubles convey no GPU authority."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disclosure_anchor.cli import m6_service_batch as cli
from disclosure_anchor.adapters.runtime.m6_service_batch import ServiceBatchFailure, ServiceBatchResult
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.parser import ParserOptions


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


class AfterReadStream:
    """Delegate a real owned stream and mutate only after its actual read."""

    def __init__(self, stream, action):
        self.stream, self.action = stream, action

    def read(self, *args):
        raw = self.stream.read(*args)
        self.action()
        return raw

    def fileno(self):
        return self.stream.fileno()

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in exception_leaves(child)]
    return [error]


class ServiceBatchCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='independent-batch-cli-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request_path = self.root / 'request.json'
        self.now = 100
        self.clock = SimpleNamespace(identity_sha256='sha256:' + 'a' * 64, now_ns=lambda: self.now)
        self.payload = {
            'contract_version': 'm6.service-batch-request.v1', 'batch_id': 'batch-literal',
            'inputs': [{'attempt_id': 'attempt-a', 'fence_identity': 'fence-a',
                        'submission_epoch_unix': 123456, 'input_pdf': str(self.root / 'source.pdf'),
                        'source_pdf_sha256': 'sha256:' + 'b' * 64,
                        'source_byte_count': 321, 'source_page_count': 2}],
            'api_url': 'http://api.invalid:8000', 'server_url': 'http://vlm.invalid:9000',
            'options': {'backend': 'hybrid-http-client', 'effort': 'medium', 'language': 'ch',
                        'formula': True, 'table': True,
                        'runtime_bundle_identity_sha256': 'sha256:' + 'c' * 64},
            'journal_root': str(self.root / 'original-journal'),
            'clock_identity_sha256': self.clock.identity_sha256, 'deadline_ns': 1000,
            'max_in_flight': 2,
            'credits_limit': {'documents': 2, 'snapshot_items': 2, 'snapshot_bytes': 642,
                              'remote_waits': 2, 'provider_tasks': 2, 'provider_result_bytes': 536870912,
                              'materialization_items': 2, 'compressed_bytes': 536870912,
                              'decoded_bytes': 8589934592, 'temp_disk_bytes': 36000000000,
                              'output_items': 2, 'output_bytes': 34359738368, 'output_pages': 4,
                              'ack_items': 2},
        }
        self.write()

    def write(self, value=None):
        self.request_path.write_bytes(canonical(self.payload if value is None else value))

    def result(self, *, not_dispatched=(), unreconciled=()):
        return ServiceBatchResult(
            controller=None, previously_disposed=(), not_dispatched=not_dispatched,
            retained_or_unresolved_credits=ResourceCreditVector(temp_disk_bytes=23),
            unreconciled=unreconciled,
        )

    def test_original_canonical_request_is_read_without_mutation_or_normalization(self):
        self.payload['batch_id'] = '原批次'
        self.write()
        raw = self.request_path.read_bytes()
        result = cli.read_batch_request(self.request_path)
        self.assertEqual(result, self.payload)
        result['inputs'][0]['attempt_id'] = 'changed-return'
        self.assertEqual(cli.read_batch_request(self.request_path), self.payload)
        self.assertEqual(self.request_path.read_bytes(), raw)

    def test_request_closed_version_and_canonical_json_are_not_coerced(self):
        values = [[], {**self.payload, 'contract_version': 'm6.service-batch-request.v2'},
                  {**self.payload, 'unknown': None}]
        values.extend({key: value for key, value in self.payload.items() if key != missing}
                      for missing in self.payload)
        values.extend({**self.payload, field: False} for field in ('inputs', 'options', 'credits_limit'))
        invalid_raw = [canonical(value) for value in values]
        raw = canonical(self.payload)
        invalid_raw.extend((raw + b'\n', json.dumps(self.payload, indent=2).encode(),
                            b'{"batch_id":"duplicate",' + raw[1:], b'\xff',
                            raw.replace(b'1000', b'NaN')))
        for raw in invalid_raw:
            with self.subTest(raw=raw[:100]):
                self.request_path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    cli.read_batch_request(self.request_path)

    def test_request_exact_megabyte_boundary_and_oversize_refusal_precede_json(self):
        maximum = 1024 * 1024
        payload = deepcopy(self.payload)
        padding = maximum - len(canonical(payload))
        payload['api_url'] += 'x' * padding
        self.assertEqual(len(canonical(payload)), maximum)
        self.write(payload)
        self.assertEqual(cli.read_batch_request(self.request_path), payload)
        # Exact-length acceptance here is only the request reader's byte bound;
        # the oversized URL is not asserted to be a valid runtime submission.
        for raw in (b'', canonical(payload) + b' '):
            with self.subTest(size=len(raw)):
                self.request_path.write_bytes(raw)
                with patch.object(cli, 'strict_json_loads') as decoder:
                    with self.assertRaises(ValueError):
                        cli.read_batch_request(self.request_path)
                decoder.assert_not_called()

    def test_named_symlink_hardlink_directory_and_fifo_are_rejected_without_wait(self):
        link = self.root / 'symlink.json'
        link.symlink_to(self.request_path)
        directory = self.root / 'directory.json'
        directory.mkdir()
        fifo = self.root / 'pipe.json'
        os.mkfifo(fifo)
        for path in (link, directory, fifo):
            with self.subTest(kind=path.name):
                with self.assertRaises((ValueError, OSError)):
                    cli.read_batch_request(path)
        hardlink = self.root / 'hardlink.json'
        os.link(self.request_path, hardlink)
        with self.assertRaises(ValueError):
            cli.read_batch_request(self.request_path)

    def test_request_path_replacement_during_read_is_rejected_and_original_fd_closes(self):
        replacement = self.root / 'replacement.json'
        replacement.write_bytes(self.request_path.read_bytes())
        original_fdopen = os.fdopen
        seen = []

        def observe_read(fd, *args, **kwargs):
            seen.append(fd)
            return AfterReadStream(original_fdopen(fd, *args, **kwargs),
                                   lambda: os.replace(replacement, self.request_path))

        with patch.object(cli.os, 'fdopen', observe_read):
            with self.assertRaisesRegex(ValueError, 'changed'):
                cli.read_batch_request(self.request_path)
        self.assertEqual(len(seen), 1)
        with self.assertRaises(OSError):
            os.fstat(seen[0])
        self.assertEqual(self.request_path.read_bytes(), canonical(self.payload))

    def test_same_inode_growth_and_same_size_rewrite_are_detected_after_read(self):
        original_fdopen = os.fdopen
        for mutation in (lambda raw: raw + b' ', lambda raw: raw.replace(b'batch-literal', b'batch-mutated')):
            with self.subTest(mutation=mutation):
                self.write()
                initial_inode = self.request_path.stat().st_ino
                count = 0

                def mutate_after_read():
                    nonlocal count
                    count += 1
                    self.request_path.write_bytes(mutation(self.request_path.read_bytes()))

                def observe_read(fd, *args, **kwargs):
                    return AfterReadStream(original_fdopen(fd, *args, **kwargs), mutate_after_read)

                with patch.object(cli.os, 'fdopen', observe_read):
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        cli.read_batch_request(self.request_path)
                self.assertEqual(count, 1)
                self.assertEqual(self.request_path.stat().st_ino, initial_inode)

    def test_fdopen_constructor_failure_closes_the_exact_raw_descriptor_and_preserves_error(self):
        original_open = os.open
        descriptors = []
        failure = OSError('owned request wrapper construction failed')

        def record_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            descriptors.append(fd)
            return fd

        try:
            with patch.object(cli.os, 'open', record_open), patch.object(cli.os, 'fdopen', side_effect=failure):
                with self.assertRaises(OSError) as captured:
                    cli.read_batch_request(self.request_path)
            self.assertIs(captured.exception, failure)
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
        finally:
            # Reap only the test's own fd if a pre-fix source leaked it. This
            # cleanup is after the assertion and cannot turn that failure green.
            for fd in descriptors:
                try:
                    os.fstat(fd)
                except OSError:
                    continue
                os.close(fd)

    def test_signal_restore_failure_attempts_both_handlers_and_preserves_primary_and_each_error(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        original_signal = signal.signal
        for failing in ((signal.SIGINT,), (signal.SIGINT, signal.SIGTERM)):
            with self.subTest(failing=failing):
                restores = []
                cleanup = {number: OSError(f'cannot restore {number}') for number in failing}
                primary = ServiceBatchFailure(self.result(unreconciled=('attempt-a',)))
                runtime_error = RuntimeError('original runtime cause')
                primary.__cause__ = runtime_error
                errors = io.StringIO()

                def install(number, handler):
                    if handler is previous[number]:
                        restores.append(number)
                        if number in cleanup:
                            raise cleanup[number]
                    return original_signal(number, handler)

                try:
                    with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                            patch.object(cli, 'run_service_batch', side_effect=primary), \
                            patch.object(cli.signal, 'signal', install), redirect_stderr(errors):
                        with self.assertRaises(BaseException) as captured:
                            cli.main(['--request', str(self.request_path)])
                    self.assertEqual(restores, [signal.SIGINT, signal.SIGTERM])
                    self.assertIsInstance(captured.exception, BaseExceptionGroup)
                    self.assertEqual(exception_leaves(captured.exception), [primary, *(cleanup[n] for n in failing)])
                    self.assertIs(primary.__cause__, runtime_error)
                    if signal.SIGTERM not in cleanup:
                        self.assertIs(signal.getsignal(signal.SIGTERM), previous[signal.SIGTERM])
                    self.assertEqual(json.loads(errors.getvalue())['result']['unreconciled'], ['attempt-a'])
                finally:
                    # The intentionally failed restore cannot repair itself;
                    # the fixture independently restores exact pre-test handlers.
                    for number, handler in previous.items():
                        original_signal(number, handler)

    def test_main_preserves_original_inputs_credits_clock_deadline_and_explicit_resume(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                output = io.StringIO()

                def run(**kwargs):
                    self.assertEqual(kwargs['batch_id'], self.payload['batch_id'])
                    self.assertEqual(kwargs['api_url'], self.payload['api_url'])
                    self.assertEqual(kwargs['server_url'], self.payload['server_url'])
                    self.assertEqual(kwargs['deadline_ns'], 1000)
                    self.assertEqual(kwargs['clock_identity_sha256'], self.clock.identity_sha256)
                    self.assertEqual(kwargs['continuous_ns'](), 100)
                    self.assertEqual(kwargs['journal_root'], self.root / 'original-journal')
                    self.assertIs(kwargs['resume'], resume)
                    self.assertEqual(kwargs['max_in_flight'], 2)
                    self.assertEqual(asdict(kwargs['credits_limit']), self.payload['credits_limit'])
                    self.assertIs(type(kwargs['options']), ParserOptions)
                    for key, value in self.payload['options'].items():
                        self.assertEqual(getattr(kwargs['options'], key), value)
                    self.assertIs(type(kwargs['inputs']), tuple)
                    actual = asdict(kwargs['inputs'][0])
                    actual['input_pdf'] = str(actual['input_pdf'])
                    self.assertEqual(actual, self.payload['inputs'][0])
                    self.assertFalse(kwargs['stop_requested']())
                    self.assertIsNone(kwargs['before_submit']())
                    return self.result()

                with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                        patch.object(cli, 'run_service_batch', side_effect=run) as runtime, redirect_stdout(output):
                    argv = ['--request', str(self.request_path)] + (['--resume'] if resume else [])
                    self.assertEqual(cli.main(argv), 0)
                runtime.assert_called_once()
                wire = json.loads(output.getvalue())
                self.assertEqual(wire['contract_version'], 'm6.service-batch-result.v1')
                self.assertEqual(wire['result']['qualification_scope'], 'functional_lifecycle_only_quality_unverified')
                self.assertNotIn('service_validated_source_pages', wire['result'])
                self.assertFalse((self.root / 'original-journal').exists())

    def test_clock_mismatch_expiry_and_noninteger_deadline_never_call_runtime(self):
        invalid = [{'clock_identity_sha256': 'sha256:' + 'd' * 64}]
        invalid.extend({'deadline_ns': value} for value in (100, 99, 0, -1, True, False, 1000.0, '1000', None))
        for delta in invalid:
            with self.subTest(delta=delta):
                self.write({**self.payload, **delta})
                with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                        patch.object(cli, 'run_service_batch') as runtime:
                    with self.assertRaises(ValueError):
                        cli.main(['--request', str(self.request_path), '--resume'])
                runtime.assert_not_called()

    def test_typed_nested_constructor_errors_precede_runtime_and_signal_installation(self):
        changes = [({'options': {**self.payload['options'], 'foreign': True}}),
                   {'credits_limit': {**self.payload['credits_limit'], 'foreign': 1}},
                   {'credits_limit': {**self.payload['credits_limit'], 'documents': True}}]
        changes.extend({'inputs': [{**self.payload['inputs'][0], key: value}]} for key, value in (
            ('foreign', 'x'), ('source_page_count', True), ('source_byte_count', 0), ('input_pdf', 'relative.pdf')))
        for delta in changes:
            with self.subTest(delta=delta):
                self.write({**self.payload, **delta})
                with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                        patch.object(cli, 'run_service_batch') as runtime, patch.object(cli.signal, 'signal') as install:
                    with self.assertRaises((TypeError, ValueError)):
                        cli.main(['--request', str(self.request_path)])
                runtime.assert_not_called()
                install.assert_not_called()

    def test_not_dispatched_or_unreconciled_returns_two_with_visible_partial_report(self):
        for pending, unresolved in ((('attempt-a',), ()), ((), ('attempt-a',)), (('attempt-a',), ('unknown-b',))):
            with self.subTest(pending=pending, unresolved=unresolved):
                output = io.StringIO()
                with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                        patch.object(cli, 'run_service_batch', return_value=self.result(
                            not_dispatched=pending, unreconciled=unresolved)), redirect_stdout(output):
                    self.assertEqual(cli.main(['--request', str(self.request_path)]), 2)
                wire = json.loads(output.getvalue())['result']
                self.assertEqual(wire['not_dispatched'], list(pending))
                self.assertEqual(wire['unreconciled'], list(unresolved))
                self.assertEqual(wire['retained_or_unresolved_credits']['temp_disk_bytes'], 23)

    def test_real_sigint_and_sigterm_latch_stop_and_handlers_restore(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        for number in previous:
            with self.subTest(signal=number):
                def run(**kwargs):
                    self.assertFalse(kwargs['stop_requested']())
                    signal.raise_signal(number)
                    self.assertTrue(kwargs['stop_requested']())
                    with self.assertRaisesRegex(RuntimeError, 'admission is closed'):
                        kwargs['before_submit']()
                    return self.result(not_dispatched=('attempt-a',))

                with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                        patch.object(cli, 'run_service_batch', side_effect=run), redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--request', str(self.request_path)]), 2)
                self.assertEqual({item: signal.getsignal(item) for item in previous}, previous)

    def test_post_guard_rechecks_original_deadline_without_renewal(self):
        def run(**kwargs):
            self.now = 999
            kwargs['before_submit']()
            self.now = 1000
            with self.assertRaisesRegex(RuntimeError, 'original deadline'):
                kwargs['before_submit']()
            self.assertEqual(kwargs['deadline_ns'], 1000)
            return self.result(not_dispatched=('attempt-a',))

        with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                patch.object(cli, 'run_service_batch', side_effect=run), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--request', str(self.request_path), '--resume']), 2)
        self.assertEqual(json.loads(self.request_path.read_bytes())['deadline_ns'], 1000)

    def test_batch_failure_emits_partial_stderr_preserves_exact_exception_and_restores_signals(self):
        primary = OSError('owned runtime failure')
        failure = ServiceBatchFailure(self.result(unreconciled=('attempt-a',)))
        failure.__cause__ = ExceptionGroup('original', [primary])
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                patch.object(cli, 'run_service_batch', side_effect=failure), \
                redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(ServiceBatchFailure) as captured:
                cli.main(['--request', str(self.request_path)])
        self.assertIs(captured.exception, failure)
        self.assertIs(captured.exception.__cause__.exceptions[0], primary)
        self.assertEqual(output.getvalue(), '')
        partial = json.loads(errors.getvalue())
        self.assertEqual(partial['contract_version'], 'm6.service-batch-result.v1')
        self.assertEqual(partial['result']['unreconciled'], ['attempt-a'])
        self.assertEqual(partial['result']['qualification_scope'], 'functional_lifecycle_only_quality_unverified')
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_unexpected_runtime_error_is_not_converted_into_success_and_restores_signals(self):
        failure = RuntimeError('unexpected before result')
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        output = io.StringIO()
        with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                patch.object(cli, 'run_service_batch', side_effect=failure), redirect_stdout(output):
            with self.assertRaises(RuntimeError) as captured:
                cli.main(['--request', str(self.request_path)])
        self.assertIs(captured.exception, failure)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_partial_signal_installation_failure_restores_first_handler_without_runtime(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        original_signal = signal.signal
        failure = OSError('second handler installation failed')
        calls = 0

        def install(number, handler):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise failure
            return original_signal(number, handler)

        with patch.object(cli, 'diagnostic_continuous_clock', return_value=self.clock), \
                patch.object(cli.signal, 'signal', install), patch.object(cli, 'run_service_batch') as runtime:
            with self.assertRaises(OSError) as captured:
                cli.main(['--request', str(self.request_path)])
        self.assertIs(captured.exception, failure)
        runtime.assert_not_called()
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_cli_requires_request_and_rejects_undeclared_flags_before_reading(self):
        for arguments in ([], ['--request', str(self.request_path), '--new-deadline', '9999']):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), \
                    patch.object(cli, 'read_batch_request') as reader, patch.object(cli, 'run_service_batch') as runtime:
                with self.assertRaises(SystemExit) as captured:
                    cli.main(arguments)
                self.assertEqual(captured.exception.code, 2)
                reader.assert_not_called()
                runtime.assert_not_called()


if __name__ == '__main__':
    unittest.main()
