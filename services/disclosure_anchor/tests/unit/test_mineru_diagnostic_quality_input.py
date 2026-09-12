"""Independent literal original-input and request data contracts; no IO authority."""

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
import os
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.mineru_diagnostic_quality import RetainedFileSeal
from disclosure_anchor.application.contracts.mineru_diagnostic_quality_config import OwnedDiagnosticQualityConfig
from disclosure_anchor.application.contracts.mineru_diagnostic_quality_input import (
    QualityChildRequest, QualityInputManifest, QualityOutputEntry, QualitySourceSeal,
    decode_quality_input_value, encode_quality_input_value,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from tests._mineru_quality_config_fixture import canonical, sha
from tests._mineru_quality_input_fixture import (
    DIRECTORY, E1_RECORD_BYTES, I63, LIFETIME_NS, REGULAR, inventory_at_bytes, inventory_payload, manifest_payload,
    output_payload, rehash_inventory, request_payload, source_payload, target_payload,
)


def records():
    return ((QualitySourceSeal, source_payload()), (QualityOutputEntry, output_payload()),
            (QualityInputManifest, manifest_payload()), (QualityChildRequest, request_payload()))


def construct(record_type, payload):
    arguments = deepcopy(payload)
    if record_type in (QualitySourceSeal, QualityOutputEntry):
        arguments["identity"] = tuple(arguments["identity"])
    elif record_type is QualityInputManifest:
        for name in ("journal_root_identity", "resources_identity"):
            arguments[name] = tuple(arguments[name])
        arguments["snapshot_seal"] = construct(QualitySourceSeal, arguments["snapshot_seal"])
        arguments["output_inventory"] = tuple(construct(QualityOutputEntry, item) for item in arguments["output_inventory"])
        arguments["target_identity"] = ParserTargetIdentity(**arguments["target_identity"])
        arguments["configuration"] = OwnedDiagnosticQualityConfig.from_payload(arguments["configuration"])
    else:
        for name in ("retained_root_identity", "resources_identity"):
            arguments[name] = tuple(arguments[name])
        seal = arguments["input_file"]
        seal["identity"] = tuple(seal["identity"])
        arguments["input_file"] = RetainedFileSeal(**seal)
    return record_type(**arguments)


class QualityInputContractTests(unittest.TestCase):
    def reject(self, record_type, payload):
        with self.assertRaises((ValueError, TypeError)):
            record_type.from_payload(payload)

    def test_four_literal_direct_constructors_payloads_and_exact_canonical_codec_roundtrips(self):
        for record_type, expected in records():
            with self.subTest(record=record_type.__name__):
                original = deepcopy(expected)
                value = construct(record_type, expected)
                self.assertEqual(value.to_payload(), original)
                self.assertEqual(record_type.from_payload(expected), value)
                self.assertEqual(record_type.from_payload(value.to_payload()), value)
                raw = canonical(original)
                self.assertEqual(encode_quality_input_value(value, maximum_bytes=len(raw)), raw)
                self.assertEqual(decode_quality_input_value(raw, record_type, maximum_bytes=len(raw)), value)
                self.assertEqual(expected, original)
                self.assertFalse(hasattr(value, "__dict__"))
                with self.assertRaises((FrozenInstanceError, AttributeError)):
                    setattr(value, fields(value)[0].name, None)

    def test_wire_arrays_are_detached_immutable_tuples_with_exact_nested_value_types(self):
        expected = manifest_payload()
        value = QualityInputManifest.from_payload(expected)
        self.assertIs(type(value.snapshot_seal), QualitySourceSeal)
        self.assertIs(type(value.output_inventory), tuple)
        self.assertTrue(all(type(entry) is QualityOutputEntry for entry in value.output_inventory))
        self.assertIs(type(value.journal_root_identity), tuple)
        self.assertIs(type(value.snapshot_seal.identity), tuple)
        self.assertIs(type(value.configuration), OwnedDiagnosticQualityConfig)
        self.assertIs(type(value.target_identity), ParserTargetIdentity)
        raw = canonical(expected)
        expected["snapshot_seal"]["identity"][1] = 888
        expected["output_inventory"].clear()
        projected = value.to_payload()
        projected["output_inventory"][0]["identity"][1] = 999
        self.assertEqual(encode_quality_input_value(value, maximum_bytes=len(raw)), raw)
        request = QualityChildRequest.from_payload(request_payload())
        self.assertIs(type(request.input_file), RetainedFileSeal)
        self.assertIs(type(request.resources_identity), tuple)

    def test_every_required_wire_field_is_closed_without_defaults_or_unknown_authority_fields(self):
        class MappingSubclass(dict):
            pass

        for record_type, payload in records():
            for missing in payload:
                with self.subTest(record=record_type.__name__, missing=missing):
                    self.reject(record_type, {key: value for key, value in payload.items() if key != missing})
            for value in (None, [], tuple(payload.items()), MappingSubclass(payload), {**payload, "verified": True},
                          {**payload, "argv": ["alternate_worker"]}, {**payload, "env": {"PATH": "/invented"}},
                          {**payload, "producer_build": {}}):
                with self.subTest(record=record_type.__name__, shape=type(value).__name__):
                    self.reject(record_type, value)
        for version, record_type, payload in (
            ("mineru-owned-quality.input.v2", QualityInputManifest, manifest_payload()),
            ("mineru-owned-quality.request.v2", QualityChildRequest, request_payload()),
        ):
            self.reject(record_type, {**payload, "contract_version": version})
        for field in ("snapshot_seal", "configuration", "target_identity"):
            payload = manifest_payload()
            payload[field]["authority"] = True
            with self.subTest(nested=field):
                self.reject(QualityInputManifest, payload)
        payload = request_payload()
        payload["input_file"]["self_sha256"] = sha(b"unrequested")
        self.reject(QualityChildRequest, payload)

    def test_identity_components_use_exact_int63_nonzero_inode_and_exact_private_modes(self):
        for record_type, original, key, mode in (
            (QualitySourceSeal, source_payload(), "identity", REGULAR),
            (QualityOutputEntry, output_payload(), "identity", REGULAR),
            (QualityInputManifest, manifest_payload(), "journal_root_identity", DIRECTORY),
            (QualityInputManifest, manifest_payload(), "resources_identity", DIRECTORY),
            (QualityChildRequest, request_payload(), "retained_root_identity", DIRECTORY),
            (QualityChildRequest, request_payload(), "resources_identity", DIRECTORY),
        ):
            for identity in ([0, 1, mode, 0], [I63, I63, mode, I63]):
                payload = deepcopy(original)
                payload[key] = identity
                with self.subTest(record=record_type.__name__, key=key, identity=identity):
                    self.assertEqual(record_type.from_payload(payload).to_payload()[key], identity)
            for wrong in ([], [1, 2, mode], [1, 2, mode, 3, 4], (1, 2, mode, 3), "identity"):
                self.reject(record_type, {**original, key: wrong})
            for index in range(4):
                for wrong in (False, True, -1, I63 + 1, 1.0, "1", None):
                    payload = deepcopy(original)
                    payload[key][index] = wrong
                    with self.subTest(record=record_type.__name__, key=key, index=index, value=wrong):
                        self.reject(record_type, payload)
            payload = deepcopy(original)
            payload[key][1] = 0
            self.reject(record_type, payload)
            for wrong_mode in (0o600, 0o700, 0o100644, 0o40755, 0o120600, 0o10600,
                               DIRECTORY if mode == REGULAR else REGULAR):
                payload = deepcopy(original)
                payload[key][2] = wrong_mode
                self.reject(record_type, payload)

    def test_positive_source_counts_zero_output_file_and_directory_null_fields_are_distinct(self):
        for record_type, original, field in ((QualitySourceSeal, source_payload(), "bytes"),
                                             (QualityInputManifest, manifest_payload(), "source_page_count")):
            for count in (1, I63):
                self.assertEqual(record_type.from_payload({**original, field: count}).to_payload()[field], count)
            for invalid in (0, -1, True, False, 1.0, "1", None, I63 + 1):
                self.reject(record_type, {**original, field: invalid})
        for count in (0, 1, I63):
            value = QualityOutputEntry.from_payload({**output_payload(), "bytes": count})
            self.assertEqual(value.bytes, count)
        for invalid in (-1, True, 1.0, "0", None, I63 + 1):
            self.reject(QualityOutputEntry, {**output_payload(), "bytes": invalid})
        directory = inventory_payload()[1]
        self.assertEqual(QualityOutputEntry.from_payload(directory).to_payload(), directory)
        for fields_ in ({"bytes": 0}, {"sha256": sha(b"")}, {"bytes": 0, "sha256": sha(b"")}):
            self.reject(QualityOutputEntry, {**directory, **fields_})
        self.reject(QualityOutputEntry, {**output_payload(), "sha256": None})

    def test_original_attempt_grammar_retains_unicode_and_exact_128_character_boundary(self):
        for value in ("a", "独" * 128, "a" * 128, "a\x7f", "😀" * 128):
            payload = {**manifest_payload(), "attempt_id": value}
            self.assertEqual(QualityInputManifest.from_payload(payload).attempt_id, value)
            self.assertEqual(construct(QualityInputManifest, payload).attempt_id, value)
        for value in ("", "a" * 129, "a b", "\tvalue", "a\n", "a\x00", 1, None, True):
            self.reject(QualityInputManifest, {**manifest_payload(), "attempt_id": value})
        for value in ("x\ud800", "x\udfff"):
            payload = {**manifest_payload(), "attempt_id": value}
            with self.subTest(kind="direct constructor", escaped=ascii(value)), self.assertRaises((ValueError, TypeError)):
                construct(QualityInputManifest, payload)
            with self.subTest(kind="from_payload", escaped=ascii(value)):
                self.reject(QualityInputManifest, payload)

    def test_original_finite_lifetime_is_not_reset_and_accepts_exact_two_hour_boundary(self):
        for started, deadline in ((0, 1), (0, LIFETIME_NS), (I63 - LIFETIME_NS, I63)):
            payload = {**manifest_payload(), "started_ns": started, "deadline_ns": deadline}
            value = QualityInputManifest.from_payload(payload)
            self.assertEqual((value.started_ns, value.deadline_ns), (started, deadline))
        for started, deadline in ((0, LIFETIME_NS + 1), (100, 100), (101, 100), (-1, 100), (0, 0),
                                  (False, 100), (1.0, 100), (0, True), (0, I63 + 1)):
            self.reject(QualityInputManifest, {**manifest_payload(), "started_ns": started, "deadline_ns": deadline})

    def test_inventory_hash_preserves_original_unsorted_child_before_parent_array(self):
        payload = manifest_payload()
        literal_names = ["output/parser/page.json", "output/empty", "output/outside.bin", "output/parser",
                         "output/zero.bin", "output"]
        self.assertEqual([item["path"] for item in payload["output_inventory"]], literal_names)
        original_digest = sha(canonical(payload["output_inventory"]))
        self.assertEqual(payload["output_inventory_sha256"], original_digest)
        value = QualityInputManifest.from_payload(payload)
        self.assertEqual([item.path for item in value.output_inventory], literal_names)
        self.assertEqual(value.to_payload()["output_inventory"], payload["output_inventory"])
        reverse = deepcopy(payload)
        reverse["output_inventory"].reverse()
        self.reject(QualityInputManifest, reverse)
        rehash_inventory(reverse)
        self.assertNotEqual(reverse["output_inventory_sha256"], original_digest)
        other = QualityInputManifest.from_payload(reverse)
        self.assertEqual([item.path for item in other.output_inventory], literal_names[::-1])
        self.assertNotEqual(encode_quality_input_value(value, maximum_bytes=100_000),
                            encode_quality_input_value(other, maximum_bytes=100_000))

    def test_inventory_requires_unique_directory_root_and_all_directory_parents_after_rehash(self):
        inventory = inventory_payload()
        variants = [[], inventory + [deepcopy(inventory[0])], inventory[:-1],
                    [item for item in inventory if item["path"] != "output/parser"]]
        wrong_root = deepcopy(inventory)
        wrong_root[-1].update(identity=[17, 206, REGULAR, 501], bytes=0, sha256=sha(b""))
        variants.append(wrong_root)
        file_parent = deepcopy(inventory)
        file_parent[3].update(identity=[17, 204, REGULAR, 501], bytes=0, sha256=sha(b""))
        variants.append(file_parent)
        for index, entries in enumerate(variants):
            payload = manifest_payload()
            payload["output_inventory"] = entries
            with self.subTest(case=index):
                self.reject(QualityInputManifest, rehash_inventory(payload))
        root_only = manifest_payload()
        root_only["output_inventory"] = [deepcopy(inventory[-1])]
        expected = rehash_inventory(root_only)
        self.assertEqual(QualityInputManifest.from_payload(expected).to_payload(), expected)

    def test_output_paths_are_canonical_under_output_and_utf8_bound_is_not_character_count(self):
        valid = "output/" + "中" * 1363
        self.assertEqual(len(valid.encode()), 4096)
        self.assertEqual(QualityOutputEntry.from_payload({**output_payload(), "path": valid}).path, valid)
        for path in ("", "/output/a", "other/a", "outputs/a", "output/", "output//a", "output/./a",
                     "output/../a", "output/a/", "output/a\\b", "output/a\x00", valid + "x", "output/\ud800"):
            self.reject(QualityOutputEntry, {**output_payload(), "path": path})
        for path in ("output/space allowed", "output/金额"):
            self.assertEqual(QualityOutputEntry.from_payload({**output_payload(), "path": path}).path, path)

    def test_original_hash_references_require_exact_lowercase_domain_without_cross_domain_guessing(self):
        payload = manifest_payload()
        reference_fields = [key for key in payload if key.endswith("sha256") and key != "output_inventory_sha256"]
        wrong = ("a" * 64, "sha256:" + "A" * 64, "SHA256:" + "a" * 64, "sha256:" + "a" * 63,
                 "sha256:" + "g" * 64, "sha256:" + "a" * 64 + "\n", 1, None)
        for field in reference_fields:
            for value in wrong:
                with self.subTest(field=field, invalid=value):
                    self.reject(QualityInputManifest, {**payload, field: value})
        for record_type, payload in ((QualitySourceSeal, source_payload()), (QualityOutputEntry, output_payload())):
            for value in wrong:
                self.reject(record_type, {**payload, "sha256": value})
        expected = manifest_payload()
        self.assertNotEqual(expected["configuration_sha256"], sha(canonical(expected["configuration"])))
        common = sha(b"syntactically equal references carry no physical provenance")
        for field in reference_fields:
            expected[field] = common
        value = QualityInputManifest.from_payload(expected)
        self.assertTrue(all(getattr(value, field) == common for field in reference_fields))

    def test_request_requires_input_slot_complete_positive_file_for_both_exact_roles(self):
        for role in ("producer", "verifier"):
            expected = request_payload(role=role)
            self.assertEqual(QualityChildRequest.from_payload(expected).to_payload(), expected)
        others = ("producer.request.json", "producer.source.json", "producer.build.json", "producer.reads.json",
                  "producer.control.raw", "producer.stderr.raw", "verifier.request.json", "verifier.source.json",
                  "verifier.build.json", "verifier.reads.json", "verifier.control.raw", "verifier.stderr.raw",
                  "comparison.json", "qualification.json", "result.json")
        for change in ([{"slot": slot} for slot in others] + [{"evidence_kind": "failure_prefix"}, {"byte_count": 0}]):
            payload = request_payload()
            payload["input_file"].update(change)
            self.reject(QualityChildRequest, payload)
        for role in ("Producer", "", "quality", 0, False, None):
            self.reject(QualityChildRequest, {**request_payload(), "role": role})
        for work_id in ("a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 31 + "\n", 0, None):
            self.reject(QualityChildRequest, {**request_payload(), "work_id": work_id})
        for work_id in ("0" * 32, "f" * 32):
            self.assertEqual(QualityChildRequest.from_payload({**request_payload(), "work_id": work_id}).work_id, work_id)

    def test_request_paths_have_only_canonical_absolute_shape_without_invented_context_authority(self):
        for field in ("source_path", "output_path", "input_path"):
            valid = "/" + "中" * 1365
            self.assertEqual(len(valid.encode()), 4096)
            for path in ("/", valid, "/different root/任意名称"):
                self.assertEqual(QualityChildRequest.from_payload({**request_payload(), field: path}).to_payload()[field], path)
            for path in ("", "relative", "//double/root", "/root//name", "/root/./name", "/root/../name",
                         "/root/name/", "/root\\name", "/root\x00", valid + "x", "/\ud800", None, 1):
                self.reject(QualityChildRequest, {**request_payload(), field: path})
        expected = {**request_payload(), "source_path": "/one/arbitrary", "output_path": "/two/also-arbitrary",
                    "input_path": "/third/not-input-json", "resources_identity": [0, 1, DIRECTORY, 0]}
        self.assertEqual(QualityChildRequest.from_payload(expected).to_payload(), expected)

    def test_data_construction_and_codec_never_query_current_uid_files_or_replay_journal(self):
        with patch.object(os, "getuid", side_effect=AssertionError("DTO queried actual owner")), \
                patch.object(os, "stat", side_effect=AssertionError("DTO inspected a namespace")), \
                patch("builtins.open", side_effect=AssertionError("DTO opened a file")):
            for record_type, expected in records():
                value = construct(record_type, expected)
                raw = canonical(expected)
                self.assertEqual(encode_quality_input_value(value, maximum_bytes=len(raw)), raw)
                self.assertEqual(decode_quality_input_value(raw, record_type, maximum_bytes=len(raw)), value)

    def test_codec_rejects_arbitrary_types_and_subclass_projection_without_executing_it(self):
        for record_type, payload in records():
            value = construct(record_type, payload)

            class Subclass(record_type):
                def to_payload(self):
                    raise AssertionError("subclass projection executed")

            subclass = object.__new__(Subclass)
            for field in fields(value):
                object.__setattr__(subclass, field.name, getattr(value, field.name))
            for invalid in (payload, subclass, object()):
                with self.assertRaises((ValueError, TypeError)):
                    encode_quality_input_value(invalid, maximum_bytes=100_000)
            for invalid_type in (dict, object, Subclass, value):
                with self.assertRaises((ValueError, TypeError)):
                    decode_quality_input_value(canonical(payload), invalid_type, maximum_bytes=100_000)

    def test_constructors_require_exact_nested_values_and_wire_arrays_do_not_accept_tuples(self):
        manifest = construct(QualityInputManifest, manifest_payload())
        request = construct(QualityChildRequest, request_payload())
        for value, field, invalid in (
            (manifest, "snapshot_seal", source_payload()),
            (manifest, "output_inventory", list(manifest.output_inventory)),
            (manifest, "output_inventory", tuple(item.to_payload() for item in manifest.output_inventory)),
            (manifest, "configuration", manifest.configuration.to_payload()),
            (manifest, "target_identity", manifest.target_identity.to_payload()),
            (request, "input_file", request.input_file.to_payload()),
            (request, "retained_root_identity", list(request.retained_root_identity)),
        ):
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                replace(value, **{field: invalid})
        payload = manifest_payload()
        payload["output_inventory"] = tuple(payload["output_inventory"])
        self.reject(QualityInputManifest, payload)

    def test_explicit_budget_checks_precede_any_graph_projection_and_use_exact_utf8_boundary(self):
        for record_type, payload in records():
            value = construct(record_type, payload)
            raw = canonical(payload)
            for limit in (0, -1, True, False, 1.0, "1", None, 1):
                with self.subTest(record=record_type.__name__, limit=limit), patch.object(
                    record_type, "to_payload", side_effect=AssertionError("projection preceded budget rejection"),
                ) as projection:
                    with self.assertRaises((ValueError, TypeError)):
                        encode_quality_input_value(value, maximum_bytes=limit)
                    projection.assert_not_called()
            self.assertEqual(encode_quality_input_value(value, maximum_bytes=len(raw)), raw)
            for operation in (lambda: encode_quality_input_value(value, maximum_bytes=len(raw) - 1),
                              lambda: decode_quality_input_value(raw, record_type, maximum_bytes=len(raw) - 1),
                              lambda: decode_quality_input_value(raw + b"\n", record_type, maximum_bytes=len(raw) + 1)):
                with self.assertRaises((ValueError, TypeError)):
                    operation()

    def test_large_inventory_graph_is_rejected_before_entry_config_or_plan_projection(self):
        payload = manifest_payload()
        payload["output_inventory"] = [
            {"path": f"output/item-{index}", "identity": [17, 1000 + index, REGULAR, 501],
             "bytes": 0, "sha256": sha(b"")}
            for index in range(512)
        ] + [inventory_payload()[-1]]
        value = construct(QualityInputManifest, rehash_inventory(payload))
        self.assertGreater(len(canonical(payload)), 4096)
        projections = []
        with ExitStack() as stack:
            for record_type in (QualityInputManifest, QualityOutputEntry, QualitySourceSeal, OwnedDiagnosticQualityConfig):
                projections.append(stack.enter_context(patch.object(
                    record_type, "to_payload", side_effect=AssertionError("oversize graph projected"),
                )))
            for method in ("model_dump", "model_dump_json"):
                projections.append(stack.enter_context(patch.object(
                    type(value.configuration.plan), method, side_effect=AssertionError("oversize plan projected"),
                )))
            with self.assertRaises((ValueError, TypeError)):
                encode_quality_input_value(value, maximum_bytes=4096)
        self.assertTrue(all(projection.call_count == 0 for projection in projections))

    def test_encoder_revalidates_forged_frozen_values_instead_of_returning_a_repaired_record(self):
        for record_type, payload, field, invalid in (
            (QualitySourceSeal, source_payload(), "bytes", True),
            (QualityOutputEntry, output_payload(), "identity", (1, 2, DIRECTORY, 3)),
            (QualityInputManifest, manifest_payload(), "output_inventory_sha256", sha(b"different inventory")),
            (QualityChildRequest, request_payload(), "role", "alternate"),
        ):
            value = construct(record_type, payload)
            object.__setattr__(value, field, invalid)
            with self.subTest(record=record_type.__name__), self.assertRaises((ValueError, TypeError)):
                encode_quality_input_value(value, maximum_bytes=100_000)
            self.assertEqual(getattr(value, field), invalid)

    def test_direct_inventory_validation_uses_original_e1_record_bytes_without_new_count_cap(self):
        payload = manifest_payload()
        payload["output_inventory"] = inventory_at_bytes(E1_RECORD_BYTES)
        rehash_inventory(payload)
        value = construct(QualityInputManifest, payload)
        self.assertEqual(len(canonical([item.to_payload() for item in value.output_inventory])), E1_RECORD_BYTES)
        self.assertEqual(value.output_inventory_sha256, payload["output_inventory_sha256"])
        self.assertEqual([item.path for item in value.output_inventory],
                         [item["path"] for item in payload["output_inventory"]])
        payload["output_inventory"] = inventory_at_bytes(E1_RECORD_BYTES + 1)
        rehash_inventory(payload)
        with self.assertRaises((ValueError, TypeError)):
            construct(QualityInputManifest, payload)
        self.reject(QualityInputManifest, payload)

    def test_direct_target_projection_has_the_original_e1_record_bound_before_target_validation(self):
        payload = manifest_payload()
        target = target_payload()
        overhead = len(canonical(target)) - len(target["name"])
        target["name"] = "n" * (E1_RECORD_BYTES - overhead)
        self.assertEqual(len(canonical(target)), E1_RECORD_BYTES)
        payload["target_identity"] = target
        value = construct(QualityInputManifest, payload)
        self.assertEqual(value.target_identity.name, target["name"])
        target["name"] += "n"
        self.assertEqual(len(canonical(target)), E1_RECORD_BYTES + 1)
        with self.assertRaises((ValueError, TypeError)):
            construct(QualityInputManifest, payload)
        self.reject(QualityInputManifest, payload)


if __name__ == "__main__":
    unittest.main()
