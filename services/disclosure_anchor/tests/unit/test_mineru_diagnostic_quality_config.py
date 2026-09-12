"""Independent data-only config oracles; synthetic pins do not establish runtime identity."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualityPlan, M6ReasonPolicy,
)
from disclosure_anchor.application.contracts.mineru_diagnostic_quality import (
    H2ByteBudget, decode_quality_value, encode_quality_value,
)
from disclosure_anchor.application.contracts.mineru_diagnostic_quality_config import (
    OwnedDiagnosticQualityConfig, QualityCodeFilePin, QualityDependencyPin, QualityProgramPin,
    decode_quality_config_value, encode_quality_config_value,
)
from tests._mineru_quality_config_fixture import (
    DIGEST, SERVICE_CHECKS, WORKER, budget_payload, canonical, config_payload,
    dependency_payload, file_payload, plan_payload, program_payload,
)


I63 = 2**63 - 1
RECORDS = (QualityCodeFilePin, QualityDependencyPin, QualityProgramPin, OwnedDiagnosticQualityConfig)
INVALID = (TypeError, ValueError)


def literals():
    return tuple(zip(RECORDS, (file_payload(), dependency_payload(), program_payload(), config_payload()), strict=True))


def construct(record_type, payload):
    # Constructor arguments are projected from independent literal mappings.
    # No new codec/to_payload provides any expected value.
    arguments = deepcopy(payload)
    if record_type is QualityDependencyPin:
        arguments['import_names'] = tuple(arguments['import_names'])
        arguments['files'] = tuple(construct(QualityCodeFilePin, item) for item in arguments['files'])
    elif record_type is QualityProgramPin:
        for name in ('code_files', 'python_runtime_files'):
            arguments[name] = tuple(construct(QualityCodeFilePin, item) for item in arguments[name])
        arguments['dependency_pins'] = tuple(construct(QualityDependencyPin, item) for item in arguments['dependency_pins'])
    elif record_type is OwnedDiagnosticQualityConfig:
        # Existing M6 model remains its own semantic validator, not our byte oracle.
        arguments['plan'] = M6QualityPlan.model_validate_json(canonical(arguments['plan']))
        arguments['budget'] = H2ByteBudget(**arguments['budget'])
        arguments['program'] = construct(QualityProgramPin, arguments['program'])
    return record_type(**arguments)


def file_sequence(count):
    return [file_payload(f'package/f{index:05d}.py') for index in range(count)]


def dependency_sequence(count):
    return [{**dependency_payload(f'dep-{index:02d}'), 'import_names': [f'dep_{index:02d}'],
             'files': [file_payload(f'dep_{index:02d}/__init__.py')]} for index in range(count)]


def forged_record(value, **changes):
    # Deliberately bypass a frozen constructor to exercise encoder preflight.
    # This is not a fixture capability or a claim about hostile-code isolation.
    result = object.__new__(type(value))
    for field in type(value).__dataclass_fields__:
        object.__setattr__(result, field, changes.get(field, getattr(value, field)))
    return result


class QualityConfigWireTests(unittest.TestCase):
    def test_four_literal_closed_mappings_direct_roundtrip_exact_bytes_and_immutability(self):
        for record_type, expected in literals():
            with self.subTest(record=record_type.__name__):
                value = construct(record_type, expected)
                raw = canonical(expected)
                self.assertEqual(value.to_payload(), expected)
                self.assertEqual(record_type.from_payload(expected), value)
                self.assertEqual(record_type.from_payload(value.to_payload()), value)
                self.assertEqual(encode_quality_config_value(value, maximum_bytes=len(raw)), raw)
                self.assertEqual(decode_quality_config_value(raw, record_type, maximum_bytes=len(raw)), value)
                self.assertFalse(hasattr(value, '__dict__'))
                with self.assertRaises((FrozenInstanceError, AttributeError)):
                    setattr(value, next(iter(expected)), None)
                for encode in (True, False):
                    with self.subTest(encode=encode), self.assertRaises(INVALID):
                        if encode:
                            encode_quality_config_value(value, maximum_bytes=len(raw) - 1)
                        else:
                            decode_quality_config_value(raw, record_type, maximum_bytes=len(raw) - 1)

    def test_all_fields_required_unknown_claims_and_noncanonical_wire_reject(self):
        class MappingSubclass(dict):
            pass

        for record_type, expected in literals():
            malformed = [None, list(expected.items()), MappingSubclass(expected), {**expected, 'verified': True}]
            malformed += [{key: item for key, item in expected.items() if key != omitted} for omitted in expected]
            for payload in malformed:
                with self.subTest(record=record_type.__name__, payload=payload), self.assertRaises(INVALID):
                    record_type.from_payload(payload)
            raw = canonical(expected)
            for wrong in (raw + b'\n', b' ' + raw, bytearray(raw), memoryview(raw), raw.decode('utf-8')):
                with self.subTest(record=record_type.__name__, raw_type=type(wrong).__name__), self.assertRaises(INVALID):
                    decode_quality_config_value(wrong, record_type, maximum_bytes=len(raw) + 1)
        for field in ('command', 'argv', 'env', 'verified', 'coverage_complete', 'sha256'):
            with self.subTest(field=field), self.assertRaises(INVALID):
                QualityProgramPin.from_payload({**program_payload(), field: True})

    def test_nested_wire_is_closed_and_detached_without_coercing_containers(self):
        class MappingSubclass(dict):
            pass

        base = config_payload()
        value = OwnedDiagnosticQualityConfig.from_payload(base)
        frozen_expected = deepcopy(base)
        base['program']['code_files'][0]['byte_count'] = 99
        base['plan']['reason_policies'][0]['reason'] = 'changed'
        projected = value.to_payload()
        projected['program']['dependency_pins'][0]['files'].clear()
        projected['plan']['required_checks'].clear()
        self.assertEqual(value.to_payload(), frozen_expected)
        for mutate in (
            lambda p: p['program']['code_files'][0].update(verified=True),
            lambda p: p['program']['dependency_pins'][0].update(sha256=DIGEST),
            lambda p: p['plan']['reason_policies'][0].update(verified=True),
            lambda p: p['budget'].update(aggregate_prepaid=True),
            lambda p: p['program'].update(code_files=tuple(p['program']['code_files'])),
            lambda p: p['program']['dependency_pins'][0].update(import_names=('pypdfium2',)),
            lambda p: p['plan'].update(reason_policies=tuple(p['plan']['reason_policies'])),
            lambda p: p['plan'].update(required_checks=tuple(p['plan']['required_checks'])),
            lambda p: p.update(plan=MappingSubclass(p['plan'])),
            lambda p: p['plan']['reason_policies'].__setitem__(0, MappingSubclass(p['plan']['reason_policies'][0])),
        ):
            wrong = config_payload()
            mutate(wrong)
            with self.subTest(payload=wrong), self.assertRaises(INVALID):
                OwnedDiagnosticQualityConfig.from_payload(wrong)

    def test_exact_record_types_and_six_primitive_registry_remain_separate(self):
        for record_type, payload in literals():
            value = construct(record_type, payload)
            subclass = type('RecordSubclass', (record_type,), {})
            # Subclass construction is not the acceptance operation; codecs must
            # not dispatch an unregistered subclass through a base-type shortcut.
            child = object.__new__(subclass)
            for field in record_type.__dataclass_fields__:
                object.__setattr__(child, field, getattr(value, field))
            for wrong in (payload, child):
                with self.subTest(record=record_type.__name__, invalid=type(wrong).__name__), self.assertRaises(INVALID):
                    encode_quality_config_value(wrong, maximum_bytes=65536)
            for wrong_type in (dict, object, subclass, value):
                with self.subTest(record=record_type.__name__, invalid_type=wrong_type), self.assertRaises(INVALID):
                    decode_quality_config_value(canonical(payload), wrong_type, maximum_bytes=65536)
            with self.assertRaises(INVALID):
                encode_quality_value(value, maximum_bytes=65536)
            with self.assertRaises(INVALID):
                decode_quality_value(canonical(payload), record_type, maximum_bytes=65536)
        primitive = H2ByteBudget(**budget_payload())
        with self.assertRaises(INVALID):
            encode_quality_config_value(primitive, maximum_bytes=4096)


class QualityConfigPinTests(unittest.TestCase):
    def test_file_path_utf8_exact_size_and_canonical_digest_boundaries(self):
        for path in ('x', 'package/__init__.py', 'raw/libpdfium.dylib', 'data/version.json', '中' * 1365 + 'a'):
            value = QualityCodeFilePin(path, 0, DIGEST)
            self.assertEqual(value.relative_path, path)
        self.assertEqual(len(('中' * 1365 + 'a').encode()), 4096)
        self.assertEqual(QualityCodeFilePin('x', I63, DIGEST).byte_count, I63)
        for path in ('', '/absolute.py', './x.py', 'a/../x.py', 'a//x.py', 'a/./x.py', 'a/', 'a\\x.py',
                     'a\x00.py', '中' * 1365 + 'ab', 'x' * 4097):
            with self.subTest(path=path), self.assertRaises(INVALID):
                QualityCodeFilePin(path, 0, DIGEST)
        for size in (False, True, -1, I63 + 1, 1.0, '1'):
            with self.subTest(size=size), self.assertRaises(INVALID):
                QualityCodeFilePin('x', size, DIGEST)
        for digest in ('a' * 64, 'sha256:' + 'A' * 64, 'sha256:' + 'a' * 63, DIGEST + '\n', None):
            with self.subTest(digest=digest), self.assertRaises(INVALID):
                QualityCodeFilePin('x', 0, digest)
        class TextSubclass(str):
            pass
        for changes in ({'relative_path': TextSubclass('x')}, {'sha256': TextSubclass(DIGEST)}):
            with self.subTest(changes=changes), self.assertRaises(INVALID):
                QualityCodeFilePin(**{**file_payload(), **changes})

    def test_dependency_names_versions_imports_paths_and_order_are_not_normalized(self):
        for name in ('pypdfium2', 'pydantic-core', 'a' * 128):
            expected = dependency_payload(name)
            self.assertEqual(QualityDependencyPin.from_payload(expected).distribution_name, name)
        for field, wrong_values in {
            'distribution_name': ('', 'Pydantic', 'pydantic_core', 'pydantic.core', 'a--b', 'a' * 129, '中'),
            'version': ('', '1\n', '版', 'v' * 129, 1),
            'install_root': ('relative', '/a/../b', '/a//b', '/a/./b', '/a/', '/a\x00b', 'x' * 4097),
            'import_names': ([], ['b', 'a'], ['a', 'a'], ['a-b'], ['a.b'], ['1bad'], ['中'], ['a' * 257], ('valid',)),
            'files': ([], tuple(dependency_payload()['files']),
                      [file_payload('b.py'), file_payload('a.py')], [file_payload('a.py'), file_payload('a.py')]),
        }.items():
            for wrong in wrong_values:
                with self.subTest(field=field, wrong=wrong), self.assertRaises(INVALID):
                    QualityDependencyPin.from_payload({**dependency_payload(), field: wrong})
        value = QualityDependencyPin.from_payload({**dependency_payload(), 'version': 'v' * 128,
            'install_root': '/' + '中' * 1365, 'import_names': ['A', '_x', 'a1']})
        self.assertEqual(value.import_names, ('A', '_x', 'a1'))
        self.assertEqual(len(value.install_root.encode()), 4096)
        self.assertEqual(QualityDependencyPin.from_payload(
            {**dependency_payload(), 'import_names': ['a' * 256]}).import_names, ('a' * 256,))

    def test_dependency_ownership_conflicts_reject_even_when_declared_hashes_agree(self):
        a = {**dependency_payload('dep-a'), 'import_names': ['a'], 'files': [file_payload('shared.py')]}
        b = {**dependency_payload('dep-b'), 'import_names': ['b'], 'files': [file_payload('other.py')]}
        base = {**program_payload(), 'dependency_pins': [a, b]}
        self.assertEqual(len(QualityProgramPin.from_payload(base).dependency_pins), 2)
        same_file = {**b, 'files': [file_payload('shared.py')]}
        same_import = {**b, 'import_names': ['a']}
        for second in (same_file, same_import):
            with self.subTest(second=second), self.assertRaises(INVALID):
                QualityProgramPin.from_payload({**base, 'dependency_pins': [a, second]})
        # Identically spelled relative files in distinct declared roots do not
        # establish a filesystem overlap; the adapter resolves actual roots.
        separated = {**same_file, 'install_root': '/another/declared/site-packages'}
        self.assertEqual(len(QualityProgramPin.from_payload({**base, 'dependency_pins': [a, separated]}).dependency_pins), 2)

    def test_program_fixed_worker_runtime_scalars_and_declared_roots(self):
        base = program_payload()
        for field, wrong_values in {
            'contract_version': ('mineru-owned-quality.program.v2', '', None),
            'worker_module': ('os', WORKER + '.other', WORKER.upper(), ''),
            'interpreter_byte_count': (0, -1, True, 1.0, I63 + 1),
            'interpreter_sha256': ('a' * 64, 'sha256:' + 'A' * 64),
            'python_version': ('3.13', '3.13.13.extra', '3.a.1', '3.13.13\n', '3' * 33),
            'python_cache_tag': ('', 'a' * 65, '中', 'tag/313', 'tag\n'),
            'sys_platform': ('', 'a' * 33, '中', 'darwin/arm', 'darwin\n'),
        }.items():
            for wrong in wrong_values:
                with self.subTest(field=field, wrong=wrong), self.assertRaises(INVALID):
                    QualityProgramPin.from_payload({**base, field: wrong})
        for field in ('interpreter_path', 'resolved_interpreter_path', 'source_root', 'python_runtime_root'):
            for wrong in ('python', '/a//b', '/a/../b', '/a/./b', '/a\x00b', '/' + '中' * 1366):
                with self.subTest(field=field, wrong=wrong), self.assertRaises(INVALID):
                    QualityProgramPin.from_payload({**base, field: wrong})
        positive = QualityProgramPin.from_payload({**base, 'interpreter_byte_count': I63,
            'python_cache_tag': 'x' * 64, 'sys_platform': 'x' * 32})
        self.assertEqual(positive.interpreter_path, base['interpreter_path'])
        self.assertNotEqual(positive.interpreter_path, positive.resolved_interpreter_path)

    def test_pin_cardinality_exact_caps_sorted_uniqueness_and_aggregate(self):
        # This checks the protocol's finite inventory envelope without touching
        # the declared filesystem or pretending these are a required closure.
        for field, limit, factory in (
            ('code_files', 1024, file_sequence),
            ('python_runtime_files', 8192, file_sequence),
            ('dependency_pins', 32, dependency_sequence),
        ):
            base = program_payload()
            valid = factory(limit)
            self.assertEqual(len(getattr(QualityProgramPin.from_payload({**base, field: valid}), field)), limit)
            for wrong in ([], factory(limit + 1), list(reversed(valid)), [valid[0], valid[0]], tuple(valid)):
                with self.subTest(field=field, length=len(wrong)), self.assertRaises(INVALID):
                    QualityProgramPin.from_payload({**base, field: wrong})
        for field, limit, factory in (
            ('files', 8192, file_sequence),
            ('import_names', 64, lambda count: [f'm{index:03d}' for index in range(count)]),
        ):
            self.assertEqual(len(getattr(QualityDependencyPin.from_payload(
                {**dependency_payload(), field: factory(limit)}), field)), limit)
            with self.subTest(field=field), self.assertRaises(INVALID):
                QualityDependencyPin.from_payload({**dependency_payload(), field: factory(limit + 1)})
        exact = {**program_payload(), 'code_files': file_sequence(1024), 'python_runtime_files': file_sequence(8192),
                 'dependency_pins': [{**dependency_payload(), 'files': file_sequence(7168)}]}
        self.assertEqual(sum((len(exact['code_files']), len(exact['python_runtime_files']),
                              len(exact['dependency_pins'][0]['files']))), 16384)
        QualityProgramPin.from_payload(exact)
        exact['dependency_pins'][0]['files'] = file_sequence(7169)
        with self.assertRaises(INVALID):
            QualityProgramPin.from_payload(exact)

    def test_nested_python_constructors_require_exact_immutable_types(self):
        file_value = QualityCodeFilePin('a.py', 0, DIGEST)
        dependency = construct(QualityDependencyPin, dependency_payload())
        program = construct(QualityProgramPin, program_payload())
        config = construct(OwnedDiagnosticQualityConfig, config_payload())
        class FileSubclass(QualityCodeFilePin):
            pass
        class PlanSubclass(M6QualityPlan):
            pass
        class BudgetSubclass(H2ByteBudget):
            pass
        cases = (
            (QualityDependencyPin, dependency, 'files', [file_value]),
            (QualityDependencyPin, dependency, 'files', (file_payload(),)),
            (QualityDependencyPin, dependency, 'files', (FileSubclass('a.py', 0, DIGEST),)),
            (QualityDependencyPin, dependency, 'import_names', ['pypdfium2']),
            (QualityProgramPin, program, 'code_files', [file_value]),
            (QualityProgramPin, program, 'dependency_pins', [dependency]),
            (OwnedDiagnosticQualityConfig, config, 'plan', plan_payload()),
            (OwnedDiagnosticQualityConfig, config, 'plan', PlanSubclass.model_validate_json(canonical(plan_payload()))),
            (OwnedDiagnosticQualityConfig, config, 'budget', BudgetSubclass(**budget_payload())),
            (OwnedDiagnosticQualityConfig, config, 'program', program_payload()),
        )
        for record_type, value, field, wrong in cases:
            arguments = {name: getattr(value, name) for name in record_type.__dataclass_fields__}
            with self.subTest(record=record_type.__name__, field=field, type=type(wrong).__name__), self.assertRaises(INVALID):
                record_type(**{**arguments, field: wrong})

    def test_synthetic_sparse_pins_are_data_only_and_config_does_not_assert_runtime_success(self):
        # No file in /declared exists or is opened. A DTO accepts declarations;
        # actual coverage/loaded origins and journal naming require the owner.
        value = construct(OwnedDiagnosticQualityConfig, config_payload())
        self.assertEqual(len(value.program.code_files), 1)
        self.assertEqual(value.budget.retained_total_bytes, 29)
        self.assertEqual(value.plan.mode, 'service_diagnostic')
        for name in ('verified', 'coverage_complete', 'scorable', 'pid', 'qualification', 'source_observed'):
            self.assertFalse(hasattr(value, name))
        for field in ('quality_verifier', 'callback', 'result', 'owned_result', 'sha256'):
            with self.subTest(field=field), self.assertRaises(INVALID):
                OwnedDiagnosticQualityConfig.from_payload({**config_payload(), field: True})


class QualityConfigPlanBudgetTests(unittest.TestCase):
    def test_plan_existing_canonical_domain_all_dispositions_unicode_and_optional_public_check(self):
        expected = config_payload()
        for with_public in (False, True):
            payload = deepcopy(expected)
            if with_public:
                payload['plan']['required_checks'].insert(7, 'public_units_hash_match')
            value = OwnedDiagnosticQualityConfig.from_payload(payload)
            plan_raw = canonical(payload['plan'])
            self.assertEqual(value.plan.canonical_bytes(), plan_raw)
            self.assertEqual(value.plan.canonical_sha256(), 'sha256:' + hashlib.sha256(plan_raw).hexdigest())
            self.assertEqual(value.to_payload()['plan'], payload['plan'])
            self.assertEqual(encode_quality_config_value(value, maximum_bytes=len(canonical(payload))), canonical(payload))
        maximum = config_payload()
        maximum['plan']['reason_policies'] = [
            {'reason': f'{index:03d}' + '中' * 125, 'disposition': 'review_required'} for index in range(256)
        ]
        value = OwnedDiagnosticQualityConfig.from_payload(maximum)
        self.assertEqual(len(value.plan.reason_policies), 256)
        self.assertEqual(value.plan.canonical_bytes(), canonical(maximum['plan']))
        self.assertEqual(len(value.plan.reason_policies[0].reason), 128)
        empty = config_payload()
        empty['plan']['reason_policies'] = []
        self.assertEqual(OwnedDiagnosticQualityConfig.from_payload(empty).plan.reason_policies, ())

    def test_existing_m6_semantic_failures_reject_without_sorting_or_new_policy_defaults(self):
        invalid_plans = []
        for checks in (SERVICE_CHECKS[:-1], list(reversed(SERVICE_CHECKS)), SERVICE_CHECKS + [SERVICE_CHECKS[-1]],
                       [*SERVICE_CHECKS[:-1], 'invented_check']):
            invalid_plans.append({**plan_payload(), 'required_checks': checks})
        for policies in (
            [{'reason': 'a', 'disposition': 'pass'}],
            [{'reason': 'a b', 'disposition': 'review_required'}],
            [{'reason': 'a', 'disposition': 'review_required'}, {'reason': 'a', 'disposition': 'score_hard_fail'}],
            list(reversed(plan_payload()['reason_policies'])),
            [{'reason': 'a' * 129, 'disposition': 'review_required'}],
            [{'reason': f'r{index:03d}', 'disposition': 'review_required'} for index in range(257)],
        ):
            invalid_plans.append({**plan_payload(), 'reason_policies': policies})
        invalid_plans.extend(({**plan_payload(), 'mode': 'e2e_publication',
            'required_checks': sorted([*SERVICE_CHECKS, 'public_units_hash_match'])},
            {**plan_payload(), 'contract_version': 'm6.quality-plan.v2'}))
        for plan in invalid_plans:
            with self.subTest(plan=plan), self.assertRaises(INVALID):
                OwnedDiagnosticQualityConfig.from_payload({**config_payload(), 'plan': plan})

    def test_exact_model_construct_forgery_cannot_bypass_existing_plan_or_reason_validation(self):
        valid = construct(OwnedDiagnosticQualityConfig, config_payload())
        base = {'contract_version': 'm6.quality-plan.v1', 'mode': 'service_diagnostic',
                'required_checks': tuple(SERVICE_CHECKS), 'reason_policies': ()}
        for changes in (
            {'required_checks': ()}, {'required_checks': tuple(reversed(SERVICE_CHECKS))},
            {'mode': 'e2e_publication'},
            {'reason_policies': (M6ReasonPolicy.model_construct(reason='a', disposition='invented'),)},
            {'reason_policies': (M6ReasonPolicy.model_construct(reason='a b', disposition='review_required'),)},
        ):
            forged = M6QualityPlan.model_construct(**{**base, **changes})
            with self.subTest(changes=changes), self.assertRaises(INVALID):
                value = OwnedDiagnosticQualityConfig(plan=forged, budget=valid.budget, program=valid.program,
                    retained_name='attempt.quality', contract_version='mineru-owned-quality.config.v1')
                encode_quality_config_value(value, maximum_bytes=65536)

    def test_retained_basename_exact_utf8_bound_and_config_version(self):
        for name in ('attempt.quality', 'a' * 247 + '.quality', '中' * 82 + 'a.quality'):
            self.assertEqual(OwnedDiagnosticQualityConfig.from_payload({**config_payload(), 'retained_name': name}).retained_name, name)
        self.assertEqual(len(('中' * 82 + 'a.quality').encode()), 255)
        for wrong in ('', 'attempt', '/attempt.quality', 'a/b.quality', 'a\\b.quality', 'a\x00.quality',
                      'a' * 248 + '.quality', '中' * 83 + '.quality'):
            with self.subTest(name=wrong), self.assertRaises(INVALID):
                OwnedDiagnosticQualityConfig.from_payload({**config_payload(), 'retained_name': wrong})
        with self.assertRaises(INVALID):
            OwnedDiagnosticQualityConfig.from_payload({**config_payload(), 'contract_version': 'mineru-owned-quality.config.v2'})

    def test_budget_is_checked_before_projection_for_each_new_type_and_large_plan(self):
        large_file = file_payload('p' * 3000)
        dependency = {**dependency_payload(), 'files': [large_file]}
        program = {**program_payload(), 'code_files': [large_file]}
        config = {**config_payload(), 'program': program}
        config['plan']['reason_policies'] = [
            {'reason': f'{index:03d}' + 'r' * 125, 'disposition': 'review_required'} for index in range(256)
        ]
        for record_type, payload in zip(RECORDS, (large_file, dependency, program, config), strict=True):
            value = construct(record_type, payload)
            for limit in (0, 1, 128, False, -1):
                with self.subTest(record=record_type.__name__, limit=limit), patch.object(
                    record_type, 'to_payload', side_effect=AssertionError('projection before budget rejection'),
                ) as project, patch.object(M6QualityPlan, 'model_dump', side_effect=AssertionError('model dump before rejection')) as dump:
                    with self.assertRaises(INVALID):
                        encode_quality_config_value(value, maximum_bytes=limit)
                    project.assert_not_called()
                    dump.assert_not_called()

    def test_one_shared_projection_budget_cannot_be_reset_for_plan_and_program(self):
        payload = config_payload()
        payload['program']['code_files'] = [file_payload('p' * 3000)]
        payload['plan']['reason_policies'] = [
            {'reason': f'{index:03d}' + 'r' * 125, 'disposition': 'review_required'} for index in range(32)
        ]
        value = construct(OwnedDiagnosticQualityConfig, payload)
        grant = max(len(canonical(payload['plan'])), len(canonical(payload['program'])))
        self.assertLess(grant, len(canonical(payload)))
        with patch.object(OwnedDiagnosticQualityConfig, 'to_payload', side_effect=AssertionError('combined projection exceeds grant')) as project, \
                patch.object(M6QualityPlan, 'model_dump', side_effect=AssertionError('nested plan expanded')) as dump:
            with self.assertRaises(INVALID):
                encode_quality_config_value(value, maximum_bytes=grant)
            project.assert_not_called()
            dump.assert_not_called()
        # Adjacent positive: the same exact nested data fits its complete bytes.
        self.assertEqual(encode_quality_config_value(value, maximum_bytes=len(canonical(payload))), canonical(payload))

    def test_forged_program_counts_reject_before_projection_even_with_large_byte_grant(self):
        valid = construct(QualityProgramPin, program_payload())
        pins = tuple(construct(QualityCodeFilePin, item) for item in file_sequence(8193))
        dependencies = tuple(construct(QualityDependencyPin, item) for item in dependency_sequence(33))
        aggregate_dependency = construct(QualityDependencyPin, {**dependency_payload(), 'files': file_sequence(7169)})
        cases = (
            {'code_files': pins[:1025]},
            {'python_runtime_files': pins},
            {'dependency_pins': dependencies},
            {'code_files': pins[:1024], 'python_runtime_files': pins[:8192],
             'dependency_pins': (aggregate_dependency,)},
        )
        for changes in cases:
            forged = forged_record(valid, **changes)
            with self.subTest(fields=tuple(changes)), patch.object(
                QualityProgramPin, 'to_payload', side_effect=AssertionError('oversize inventory materialized'),
            ) as project:
                with self.assertRaises(INVALID):
                    encode_quality_config_value(forged, maximum_bytes=64 * 1024 * 1024)
                project.assert_not_called()

    def test_forged_dependency_counts_reject_before_file_or_import_projection(self):
        valid = construct(QualityDependencyPin, dependency_payload())
        cases = (
            {'files': tuple(construct(QualityCodeFilePin, item) for item in file_sequence(8193))},
            {'import_names': tuple(f'm{index:03d}' for index in range(65))},
        )
        for changes in cases:
            with self.subTest(fields=tuple(changes)), patch.object(
                QualityDependencyPin, 'to_payload', side_effect=AssertionError('oversize dependency materialized'),
            ) as project:
                with self.assertRaises(INVALID):
                    encode_quality_config_value(forged_record(valid, **changes), maximum_bytes=64 * 1024 * 1024)
                project.assert_not_called()

    def test_narrow_plan_bridge_does_not_enable_arbitrary_pydantic_payloads(self):
        class OtherModel(BaseModel):
            note: str
        model = OtherModel(note='not a quality plan')
        with self.assertRaises(INVALID):
            encode_quality_config_value(model, maximum_bytes=4096)
        valid = construct(OwnedDiagnosticQualityConfig, config_payload())
        with self.assertRaises(INVALID):
            OwnedDiagnosticQualityConfig(plan=model, budget=valid.budget, program=valid.program,
                retained_name='attempt.quality', contract_version='mineru-owned-quality.config.v1')


if __name__ == '__main__':
    unittest.main()
