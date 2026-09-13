"""Independent actual local E1 ownership and fixed reader tests; HTTP is simulated."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import MinerUMediumArtifactReader
from disclosure_anchor.adapters.runtime import m6_service_quality_verifier as adapter
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_inputs import HeldQualityInputs
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests._mineru_diagnostic_lifecycle_fixture import SimulatedCrash, crash_after_phase, journal_records
from tests._m6_service_verifier_fixture import (
    CHECKS, VerifierFiles, alter_archive, canonical, digest, h, leaves,
    lifecycle_fixture, open_attempt, resources_path,
)


def steps(fixture):
    return [record['step'] for record in journal_records(fixture.journal)]


def freeze_output(fixture, verifier):
    with crash_after_phase('output_sealed'):
        try:
            fixture.run(service_quality_verifier=verifier)
        except SimulatedCrash:
            return
    raise AssertionError('output_sealed was not reached')


def blank_provider(files):
    for name in tuple(files):
        if name.endswith('_content_list.json'):
            files[name] = canonical([])
        elif name.endswith(('_content_list_v2.json', '_model.json')):
            files[name] = canonical([[], []])
        elif name.endswith('_middle.json'):
            middle = json.loads(files[name])
            for page in middle['pdf_info']:
                page.update(preproc_blocks=[], para_blocks=[], discarded_blocks=[])
            files[name] = canonical(middle)


class ServiceVerifierLifecycleTest(unittest.TestCase):
    def assert_retained(self, fixture):
        self.assertTrue((resources_path(fixture) / 'source.pdf').is_file())
        self.assertTrue((resources_path(fixture) / 'output').is_dir())
        self.assertNotIn('validated', steps(fixture))
        self.assertNotIn('cleanup_intent', steps(fixture))
        self.assertEqual(fixture.ack_effects, 0)

    def test_real_held_reader_once_and_literal_four_refs_survive_disposal(self):
        for empty_blocks in (False, True):
            with self.subTest(empty_blocks=empty_blocks), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = VerifierFiles(root / 'config')
                verifier = config.load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                if empty_blocks:
                    alter_archive(fixture, blank_provider)
                reads = []
                original = MinerUMediumArtifactReader.read_pinned

                def observed_read(reader, tree, **kwargs):
                    result = original(reader, tree, **kwargs)
                    reads.append(result.document)
                    return result

                with patch.object(MinerUMediumArtifactReader, 'read_pinned', observed_read), patch.object(
                        MinerUMediumArtifactReader, 'read_with_location', side_effect=AssertionError('second path read')):
                    result = fixture.run(service_quality_verifier=verifier)
                self.assertEqual(len(reads), 1)
                self.assertEqual(len(reads[0].blocks), 0 if empty_blocks else 3)
                records = {record['step']: record for record in journal_records(fixture.journal)}
                source_ref = digest(canonical(records['source_observed']))
                output_ref = digest(canonical(records['output_sealed']))
                observation = {
                    'mode': 'service_diagnostic', 'attempt_id': 'independent-attempt',
                    'source_pdf_sha256': fixture.source_sha, 'source_byte_count': len(fixture.source_bytes),
                    'source_page_count': 2, 'provider_page_count': 2,
                    'provider_bundle_sha256': reads[0].bundle_sha256,
                    'parser_target_sha256': config.plan['parser_target_sha256'],
                    'quality_verifier_sha256': verifier.identity_sha256,
                    'source_observed_record_sha256': source_ref, 'output_sealed_record_sha256': output_ref,
                    'review_reasons': [], 'checks': [
                        {'check_id': name, 'outcome': 'pass',
                         'evidence_sha256': source_ref if name == 'source_identity' else output_ref}
                        for name in CHECKS],
                }
                evidence = {'contract_version': 'm6.service-qualification-evidence.v1',
                            'observation': observation, 'reviews': []}
                qualification = {'contract_version': 'm6.service-document-qualification.v1',
                                 'evidence_sha256': digest(canonical(evidence)),
                                 'plan_sha256': digest(canonical(config.plan)),
                                 'verdict': 'scorable', 'reasons': [], 'scorable_page_count': 2}
                self.assertEqual(result['quality'], {
                    'status': 'pass', 'reason': 'source/provider contract checks passed',
                    'report': {'evidence': evidence, 'qualification': qualification},
                    'verifier_sha256': verifier.identity_sha256})
                self.assertEqual(records['binding']['value']['service_quality'], verifier.binding_payload())
                self.assertEqual(fixture.ack_effects, 1)
                self.assertEqual(steps(fixture).count('source_probe_intent'), 1)
                self.assertFalse(resources_path(fixture).exists())

    def test_injected_reader_legacy_callback_and_wrong_target_refuse_before_post(self):
        for failure in ('reader', 'callback', 'legacy_sha', 'target'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verifier = VerifierFiles(root / 'config').load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                extras = {'reader': {'reader': MinerUMediumArtifactReader()},
                          'callback': {'quality_verifier': lambda *args: {}, 'quality_verifier_sha256': h('f')},
                          'legacy_sha': {'quality_verifier_sha256': verifier.identity_sha256},
                          'target': {'options': ParserOptions(runtime_bundle_identity_sha256=h('f'), timeout_seconds=60)}}
                with self.assertRaises((ValueError, TypeError)):
                    fixture.run(service_quality_verifier=verifier, **extras[failure])
                self.assertEqual(fixture.events, [])
                self.assertEqual(fixture.ack_effects, 0)

    def test_original_owner_required_and_latest_cache_cannot_replace_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = VerifierFiles(root / 'config').load()
            first = lifecycle_fixture(root / 'first')
            second = lifecycle_fixture(root / 'second')
            freeze_output(first, verifier)
            freeze_output(second, verifier)
            with open_attempt(first) as (resources, phases), open_attempt(second) as (_, foreign):
                with self.assertRaises(DiagnosticJournalError):
                    verifier.verify_result(resources=resources, phases=foreign)
                phases.latest.clear()
                result = verifier.verify_result(resources=resources, phases=phases)
                self.assertEqual(result.evidence.observation.source_pdf_sha256, first.source_sha)
                phases.binding['source_byte_count'] += 1
                with self.assertRaises(DiagnosticJournalError):
                    verifier.verify_result(resources=resources, phases=phases)
            self.assert_retained(first)
            self.assert_retained(second)

    def test_sealed_source_and_full_output_namespace_mutations_never_validate(self):
        for failure in ('source_same_size', 'source_growth', 'outside_file', 'empty_directory', 'payload_replace'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verifier = VerifierFiles(root / 'config').load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                freeze_output(fixture, verifier)
                resource = resources_path(fixture)
                if failure.startswith('source'):
                    path = resource / 'source.pdf'
                    raw = path.read_bytes()
                    path.write_bytes((b'!' + raw[1:]) if failure == 'source_same_size' else raw + b'x')
                elif failure == 'outside_file':
                    (resource / 'output/outside/note.bin').write_bytes(b'changed')
                elif failure == 'empty_directory':
                    (resource / 'output/outside/empty').rmdir()
                else:
                    path = next((resource / 'output').rglob('*_content_list.json'))
                    raw = path.read_bytes()
                    path.rename(path.with_suffix('.saved'))
                    path.write_bytes(raw)
                    path.chmod(0o600)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(resume=True, service_quality_verifier=verifier)
                self.assert_retained(fixture)

    def test_provider_profile_page_and_image_integrity_use_real_reader_and_validator(self):
        for failure in ('profile', 'pages', 'missing_role', 'image_bytes'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verifier = VerifierFiles(root / 'config').load()
                fixture = lifecycle_fixture(root / 'attempt-root')

                def corrupt(files):
                    if failure == 'image_bytes':
                        for name in tuple(files):
                            if name.endswith('.jpg'):
                                files[name] = b'not verified image bytes'
                        return
                    middle_name = next(name for name in files if name.endswith('_middle.json'))
                    if failure == 'missing_role':
                        del files[middle_name]
                    elif failure == 'profile':
                        middle = json.loads(files[middle_name])
                        middle['_effort'] = 'high'
                        files[middle_name] = canonical(middle)
                    else:
                        blank_provider(files)
                        middle = json.loads(files[middle_name])
                        middle['pdf_info'] = middle['pdf_info'][:1]
                        files[middle_name] = canonical(middle)
                        for name in tuple(files):
                            if name.endswith(('_model.json', '_content_list_v2.json')):
                                files[name] = canonical([[]])

                alter_archive(fixture, corrupt)
                with self.assertRaises((ValueError, ParserOutputContractError)):
                    fixture.run(service_quality_verifier=verifier)
                self.assert_retained(fixture)

    def test_post_read_mutation_and_original_deadline_are_checked_before_accepting(self):
        for failure in ('source', 'outside', 'deadline', 'raw_preimage'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verifier = VerifierFiles(root / 'config').load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                original = MinerUMediumArtifactReader.read_pinned

                def mutate_after_read(reader, tree, **kwargs):
                    result = original(reader, tree, **kwargs)
                    if failure == 'deadline':
                        fixture.now_ns = fixture.deadline_ns
                    elif failure == 'raw_preimage':
                        # A concrete corrupt decoder-return seam proves the shared raw
                        # preimage validator is called; no injected reader API is accepted.
                        document = result.document
                        first = document.pages[0]
                        block = replace(first.blocks[0], raw_item_sha256=h('0'))
                        page = replace(first, blocks=(block, *first.blocks[1:]))
                        return replace(result, document=replace(document, pages=(page, *document.pages[1:])))
                    else:
                        path = resources_path(fixture) / ('source.pdf' if failure == 'source' else 'output/outside/note.bin')
                        raw = path.read_bytes()
                        path.write_bytes(b'!' + raw[1:])
                    return result

                with patch.object(MinerUMediumArtifactReader, 'read_pinned', mutate_after_read):
                    with self.assertRaises((ValueError, ParserOutputContractError, TimeoutError, BaseExceptionGroup)) as caught:
                        fixture.run(service_quality_verifier=verifier)
                allowed = (TimeoutError,) if failure == 'deadline' else (ValueError, ParserOutputContractError)
                self.assertTrue(all(isinstance(error, allowed) for error in leaves(caught.exception)))
                self.assert_retained(fixture)

    def test_holder_closure_error_stays_visible_and_never_seals_pass(self):
        for primary_present in (False, True):
            with self.subTest(primary_present=primary_present), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verifier = VerifierFiles(root / 'config').load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                original_close = HeldQualityInputs.close
                original_read = MinerUMediumArtifactReader.read_pinned
                failure = OSError('independent holder closure failure')
                primary = OSError('independent read failure')
                closed = []

                def fail_after_actual_close(holder):
                    original_close(holder)
                    closed.append(holder)
                    raise failure

                def read_or_fail(reader, tree, **kwargs):
                    if primary_present:
                        raise primary
                    return original_read(reader, tree, **kwargs)

                with patch.object(HeldQualityInputs, 'close', fail_after_actual_close), patch.object(
                        MinerUMediumArtifactReader, 'read_pinned', read_or_fail):
                    with self.assertRaises((OSError, BaseExceptionGroup)) as caught:
                        fixture.run(service_quality_verifier=verifier)
                self.assertIn(failure, leaves(caught.exception))
                if primary_present:
                    self.assertIn(primary, leaves(caught.exception))
                self.assertTrue(closed)
                self.assert_retained(fixture)

    def test_output_sealed_resume_verifies_once_without_reprobe_or_resubmit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = VerifierFiles(root / 'config').load()
            fixture = lifecycle_fixture(root / 'attempt-root')
            freeze_output(fixture, verifier)
            reads = []
            original = MinerUMediumArtifactReader.read_pinned

            def count_read(reader, tree, **kwargs):
                result = original(reader, tree, **kwargs)
                reads.append(result.document)
                return result

            with patch.object(MinerUMediumArtifactReader, 'read_pinned', count_read), patch(
                    'disclosure_anchor.adapters.runtime.mineru_diagnostic_lifecycle.observe_diagnostic_source',
                    side_effect=AssertionError('source child repeated')):
                result = fixture.run(resume=True, service_quality_verifier=verifier)
            self.assertEqual(result['quality']['status'], 'pass')
            self.assertEqual(len(reads), 1)
            self.assertEqual(fixture.events.count(('POST', '/tasks')), 1)
            self.assertEqual(fixture.events.count(('GET', f'/tasks/{fixture.task_id}/result')), 1)

    def test_validated_and_disposed_resume_use_original_seal_without_reading_deleted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = VerifierFiles(root / 'config')
            verifier = config.load()
            fixture = lifecycle_fixture(root / 'attempt-root')
            with crash_after_phase('validated'), self.assertRaises(SimulatedCrash):
                fixture.run(service_quality_verifier=verifier)
            # An independently reloaded instance preserves the original acceptance time.
            verifier = config.load()
            with patch.object(adapter.ServiceQualityVerifier, 'verify_result', side_effect=AssertionError('verifier replayed')):
                first = fixture.run(resume=True, service_quality_verifier=verifier)
                self.assertFalse(resources_path(fixture).exists())
                before = {path.name: path.read_bytes() for path in fixture.journal.iterdir() if path.is_file()}
                event_count = len(fixture.events)
                second = fixture.run(resume=True, require_disposed=True, service_quality_verifier=verifier)
            self.assertEqual(second, first)
            self.assertEqual(len(fixture.events), event_count)
            self.assertEqual(before, {path.name: path.read_bytes() for path in fixture.journal.iterdir() if path.is_file()})

    def test_resume_binding_changes_cannot_renew_policy_context_or_deadline(self):
        for change in ('plan', 'context', 'deadline'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = VerifierFiles(root / 'config')
                verifier = config.load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                freeze_output(fixture, verifier)
                extra = {}
                if change == 'plan':
                    config.plan['reason_policies'] = [{'reason': 'review', 'disposition': 'review_required'}]
                    config.plan_path.write_bytes(canonical(config.plan))
                    verifier = config.load()
                elif change == 'context':
                    config.context['max_age_seconds'] += 1
                    verifier = config.load()
                else:
                    extra['deadline_ns'] = fixture.deadline_ns + 1
                before_events = list(fixture.events)
                with self.assertRaises(DiagnosticJournalError):
                    fixture.run(resume=True, service_quality_verifier=verifier, **extra)
                self.assertEqual(fixture.events, before_events)
                self.assert_retained(fixture)

    def test_rehashed_validated_record_cannot_invent_refs_status_or_qualification(self):
        for change in ('source_ref', 'output_ref', 'check_ref', 'attempt', 'bundle', 'status', 'qualification'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verifier = VerifierFiles(root / 'config').load()
                fixture = lifecycle_fixture(root / 'attempt-root')
                with crash_after_phase('validated'), self.assertRaises(SimulatedCrash):
                    fixture.run(service_quality_verifier=verifier)
                path = next(fixture.journal.glob('*-validated.json'))
                record = json.loads(path.read_bytes())
                quality = record['value']['quality']
                report = quality['report']
                observation = report['evidence']['observation']
                if change == 'source_ref':
                    observation['source_observed_record_sha256'] = h('0')
                elif change == 'output_ref':
                    observation['output_sealed_record_sha256'] = h('0')
                elif change == 'check_ref':
                    observation['checks'][0]['evidence_sha256'] = h('0')
                elif change == 'attempt':
                    observation['attempt_id'] = 'another-attempt'
                elif change == 'bundle':
                    observation['provider_bundle_sha256'] = h('0')
                elif change == 'status':
                    quality['status'] = 'unverified'
                else:
                    report['qualification']['scorable_page_count'] = 1
                # Rehash the evidence and outer journal payload: semantic binding is the oracle.
                report['qualification']['evidence_sha256'] = digest(canonical(report['evidence']))
                record['value_sha256'] = digest(canonical(record['value']))
                path.write_bytes(canonical(record))
                before_events = list(fixture.events)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(resume=True, service_quality_verifier=verifier)
                self.assertEqual(fixture.events, before_events)
                self.assertNotIn('cleanup_intent', steps(fixture))
                self.assertEqual(fixture.ack_effects, 0)

    def test_failed_terminal_and_absent_verifier_keep_existing_unqualified_semantics(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = lifecycle_fixture(root / 'attempt-root')
                if not failed:
                    # The unchanged legacy path forbids empty directories. Only the
                    # new holder path accepts a complete sealed inventory of them.
                    alter_archive(fixture, lambda files: files.pop('outside/empty/'))
                verifier = VerifierFiles(root / 'config').load() if failed else None
                fixture.terminal_status = 'failed' if failed else 'completed'
                with patch.object(adapter.ServiceQualityVerifier, 'verify_result', side_effect=AssertionError('unexpected verifier')):
                    result = fixture.run(service_quality_verifier=verifier)
                self.assertEqual(result['quality']['status'], 'not_applicable' if failed else 'unverified')
                self.assertEqual(fixture.ack_effects, 1)
                self.assertEqual(result['quality']['report'], {})
                if failed:
                    self.assertIsNone(result['provider'])


if __name__ == '__main__':
    unittest.main()
