"""Independent literal owner journals and source-quality records for pure replay.

Ticks, role stamps and closures are synthetic contract observations. This helper
does not run or authenticate an owner, perform artifact IO or qualify a platform.
Expected page totals are hand-derived in tests, never obtained from a reducer.
"""

from copy import deepcopy

from disclosure_anchor.application.contracts.m6_campaign import M6CorpusManifest
from disclosure_anchor.application.contracts.m6_document_qualification import M6QualificationEvidence, M6QualityPlan
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.m6_service_quality import (
    M6ServiceQualificationEvidence, M6ServiceQualityPlan,
)
from tests._m6_service_quality_fixture import (
    canonical, evidence_payload, h, legacy_evidence_payload, legacy_observation_payload,
    legacy_plan_payload, observation_payload, plan_payload, sha,
)


OWNER = h('6')
BOOT = h('7')
FREQUENCY = 1000
T0 = 10_000


def decoded(model, payload):
    return model.from_canonical_bytes(canonical(payload), maximum_bytes=1_048_576)


class RunFixture:
    def __init__(self, entries=None, *, legacy=False, phase='hour_baseline', seconds=3600,
                 carry_ids=(), policies=(), resources=None):
        entries = entries or {'a': (3, 'fresh')}
        self.entries = {label: {'source_pdf_sha256': h(label), 'source_byte_count': 1000 + pages,
                               'source_page_count': pages, 'document_id': None,
                               'stratum': 'independent-literal', 'origin': origin}
                        for label, (pages, origin) in entries.items()}
        self.manifest_payload = {'contract_version': 'm6.corpus-manifest.v1',
                                 'campaign_id': 'service-campaign', 'mode': 'service_diagnostic',
                                 'entries': sorted(deepcopy(list(self.entries.values())),
                                                   key=lambda entry: entry['source_pdf_sha256'])}
        self.manifest = decoded(M6CorpusManifest, self.manifest_payload)
        self.plan_payload = legacy_plan_payload() if legacy else plan_payload()
        self.plan_payload['reason_policies'] = deepcopy(list(policies))
        self.plan = decoded(M6QualityPlan if legacy else M6ServiceQualityPlan, self.plan_payload)
        clock = {'host_assignment_identity_sha256': h('5'), 'boot_identity_sha256': BOOT,
                 'qpc_frequency_hz': FREQUENCY,
                 'clock_domain_identity_sha256': sha(canonical({
                     'boot_identity_sha256': BOOT, 'clock_source': 'QueryPerformanceCounter',
                     'frequency_hz': FREQUENCY}))}
        self.spec_payload = {
            'contract_version': 'm6.run-spec.v1', 'run_id': 'service-run',
            'campaign_id': 'service-campaign', 'mode': 'service_diagnostic', 'phase': phase,
            'start_condition': 'resident_warm', 'clock': clock,
            'runtime': {'source_commit': '1' * 40, 'source_manifest_sha256': h('2'),
                        'runtime_bundle_identity_sha256': h('3'), 'process_profile_sha256': h('4'),
                        'worker_profile_sha256': None, 'owner_source_sha256': h('6'),
                        'gpu_device_identity_sha256': h('8'), 'deployment_qualification_sha256': h('9')},
            'manifest_sha256': sha(canonical(self.manifest_payload)), 'scope_sha256': None,
            'quality_plan_sha256': sha(canonical(self.plan_payload)), 't0_ticks': T0,
            'planned_seconds': seconds, 'deadline_ticks': T0 + seconds * FREQUENCY,
            'max_close_ticks': T0 + (seconds + 200) * FREQUENCY,
            'carry_in_attempt_ids': list(carry_ids),
            'resources': {'max_events': 128, 'max_record_bytes': 8192, 'max_log_bytes': 1_048_576,
                          'max_attempts': 16, 'max_verifier_backlog_bytes': 1_048_576,
                          'stop_admission_budget_ticks': 5000, **(resources or {})},
        }
        self.spec = decoded(M6RunSpec, self.spec_payload)
        self.legacy = legacy

    def evidence(self, label='a', attempt='attempt-a', *, changes=None, reviews=()):
        entry = self.entries[label]
        observation = legacy_observation_payload() if self.legacy else observation_payload()
        observation.update(source_pdf_sha256=entry['source_pdf_sha256'],
                           source_byte_count=entry['source_byte_count'],
                           source_page_count=entry['source_page_count'],
                           provider_page_count=entry['source_page_count'])
        if not self.legacy:
            observation['attempt_id'] = attempt
        observation.update(changes or {})
        payload = legacy_evidence_payload(observation) if self.legacy else evidence_payload(observation, reviews)
        return decoded(M6QualificationEvidence if self.legacy else M6ServiceQualificationEvidence, payload)

    def reduce(self, journal, qualifications=(), **overrides):
        from disclosure_anchor.application.services.m6_service_run_accounting import reduce_m6_service_run
        arguments = {'spec': self.spec, 'manifest': self.manifest, 'quality_plan': self.plan,
                     'journal_lines': journal.lines, 'qualifications': tuple(qualifications)}
        arguments.update(overrides)
        return reduce_m6_service_run(**arguments)


class Journal:
    def __init__(self, fixture):
        self.fixture = fixture
        self.lines = []
        self.owner_sequence = 0
        self.producer_sequences = {}
        self.owner_epoch = OWNER
        self.boot = BOOT

    def at(self, seconds):
        return T0 + seconds * FREQUENCY

    def emit(self, payload, tick, *, role=None):
        kind = payload['kind']
        if role is None:
            role = ('quality_verifier' if kind in ('document_qualified', 'verifier_drained') else
                    'service_runner' if kind in ('attempt_admitted', 'remote_accepted', 'service_validated', 'attempt_final') else
                    'owner')
        epoch = self.owner_epoch if role == 'owner' else h('e' if role == 'service_runner' else 'f')
        key = role, epoch
        sequence = self.producer_sequences.get(key, 0) + 1
        self.producer_sequences[key] = sequence
        event = {'contract_version': 'm6.producer-event.v1', 'run_id': self.fixture.spec.run_id,
                 'spec_sha256': sha(canonical(self.fixture.spec_payload)), 'producer_kind': role,
                 'producer_epoch_sha256': epoch, 'producer_sequence': sequence, 'payload': deepcopy(payload)}
        return self.receive(event, tick)

    def receive(self, event, tick):
        self.owner_sequence += 1
        record = {'contract_version': 'm6.run-event.v1', 'event': deepcopy(event),
                  'stamp': {'sequence': self.owner_sequence, 'received_qpc_ticks': tick,
                            'boot_identity_sha256': self.boot, 'owner_process_epoch_sha256': self.owner_epoch,
                            'producer_event_sha256': sha(canonical(event))}}
        self.lines.append(canonical(record) + b'\n')
        return record

    def start(self):
        self.emit({'kind': 'run_started', 'clock': self.fixture.spec_payload['clock'],
                   't0_ticks': T0, 'deadline_ticks': self.fixture.spec.deadline_ticks}, T0)
        self.emit({'kind': 'admission_opened'}, T0 + 1)

    def admit(self, label='a', attempt='attempt-a', *, tick=None, accepted=True):
        entry = self.fixture.entries[label]
        tick = self.at(1) if tick is None else tick
        self.emit({'kind': 'attempt_admitted', 'attempt_id': attempt, 'fence_identity': 'fence-' + attempt,
                   'document_id': None, 'processing_run_id': None,
                   'source_pdf_sha256': entry['source_pdf_sha256'], 'source_byte_count': entry['source_byte_count'],
                   'source_page_count': entry['source_page_count'], 'process_profile_sha256': h('4')}, tick)
        if accepted:
            self.emit({'kind': 'remote_accepted', 'attempt_id': attempt,
                       'remote_task_identity_sha256': sha(attempt.encode()),
                       'acceptance_receipt_sha256': sha(('accept:' + attempt).encode())}, tick + 1)

    def validated(self, evidence, *, tick=None, attempt=None, reference=None, bundle=None):
        return self.emit({'kind': 'service_validated', 'attempt_id': attempt or 'attempt-a',
                          'provider_bundle_sha256': bundle or evidence.observation.provider_bundle_sha256,
                          'validation_receipt_sha256': reference or evidence.canonical_sha256()},
                         self.at(2) if tick is None else tick)

    def qualified(self, evidence, *, tick=None, attempt='attempt-a', reference=None, role=None):
        return self.emit({'kind': 'document_qualified', 'attempt_id': attempt,
                          'qualification_evidence_sha256': reference or evidence.canonical_sha256()},
                         self.at(3) if tick is None else tick, role=role)

    def final(self, attempt='attempt-a', *, tick=None, failed=False, submitted=True):
        self.emit({'kind': 'attempt_final', 'attempt_id': attempt,
                   'outcome': 'failed' if failed else 'diagnostic_disposed',
                   'remote_disposition': 'consumed' if submitted else 'not_submitted',
                   'remote_receipt_sha256': sha(('ack:' + attempt).encode()) if submitted else None,
                   'remote_task_identity_sha256': sha(attempt.encode()) if submitted else None,
                   'cleanup_receipt_sha256': sha(('cleanup:' + attempt).encode())},
                  self.fixture.spec.deadline_ticks + 2000 if tick is None else tick)

    def close(self, *, stop_tick=None, close_tick=None, residual=0, drained=True):
        deadline = self.fixture.spec.deadline_ticks
        self.emit({'kind': 'stop_admission_effective'}, deadline if stop_tick is None else stop_tick)
        if drained:
            self.emit({'kind': 'verifier_drained', 'drain_receipt_sha256': h('0')}, deadline + 7000)
        self.emit({'kind': 'resources_closed', 'residual_count': residual, 'children_exited': True,
                   'ownership_receipt_sha256': h('1')}, deadline + 8000)
        tick = deadline + 10_000 if close_tick is None else close_tick
        self.emit({'kind': 'run_closed', 'tclose_ticks': tick, 'reason': 'deadline_drained'}, tick)


def simple_run(*, legacy=False, evidence_changes=None, qualify=True, validate=True,
               validation_reference=None, failed=False, missing_final=False):
    fixture = RunFixture(legacy=legacy)
    evidence = fixture.evidence(changes=evidence_changes)
    journal = Journal(fixture)
    journal.start()
    journal.admit()
    if validate:
        journal.validated(evidence, reference=validation_reference)
    if qualify:
        journal.qualified(evidence)
    journal.emit({'kind': 'stop_admission_effective'}, fixture.spec.deadline_ticks)
    if not missing_final:
        journal.final(failed=failed)
    journal.emit({'kind': 'verifier_drained', 'drain_receipt_sha256': h('0')}, fixture.spec.deadline_ticks + 7000)
    journal.emit({'kind': 'resources_closed', 'residual_count': 0, 'children_exited': True,
                  'ownership_receipt_sha256': h('1')}, fixture.spec.deadline_ticks + 8000)
    journal.emit({'kind': 'run_closed', 'tclose_ticks': fixture.spec.deadline_ticks + 10_000,
                  'reason': 'deadline_drained'}, fixture.spec.deadline_ticks + 10_000)
    return fixture, journal, evidence


def receipt_payload(*, legacy=False):
    """Literal complete-zero wire oracle, independent of either reducer."""
    return {'contract_version': 'm6.run-receipt.v1' if legacy else 'm6.service-run-receipt.v1',
            'run_id': 'service-run', 'spec_sha256': h('1'), 'journal_prefix_sha256': h('2'),
            'journal_bytes_consumed': 6000, 'mode': 'service_diagnostic', 'phase': 'hour_baseline',
            'status': 'complete', 'incomplete_reasons': [], 'invalid_reasons': [],
            't0_ticks': T0, 'deadline_ticks': T0 + 3_600_000, 'tclose_ticks': T0 + 3_610_000,
            'elapsed_ticks': 3_610_000, 'qpc_frequency_hz': 1000,
            'stop_requested_ticks': None, 'stop_effective_ticks': T0 + 3_600_000,
            'close_reason': 'deadline_drained',
            'metrics': {'kind': 'service_validated_source_pages' if legacy else 'service_provider_integrity_source_pages',
                        'window_pages': 0, 'whole_run_pages': 0, 'replay_pages': 0, 'carry_in_pages': 0},
            'sources': [], 'events_total': 6, 'duplicate_events': 0}
