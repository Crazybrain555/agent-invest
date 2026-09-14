"""Closed pressure wire with independently authored values and external identities."""

from copy import deepcopy
import json
import unittest

from disclosure_anchor.application.contracts.mineru_process_pressure import (
    parse_mineru_process_pressure,
)


CAPACITY = "sha256:" + "1" * 64
CGROUP = "sha256:" + "2" * 64
OWNER = {
    "process_id": 41,
    "process_start_ticks": 9301,
    "boot_id": "00000000-0000-4000-8000-000000000003",
    "loop_epoch": "00000000-0000-4000-8000-000000000004",
}


def pressure_payload():
    # Deliberately independent of the kernel producer serializer and DTO dumps.
    return {
        "schema": "mineru.process-pressure.v1",
        "capacity_config_sha256": CAPACITY,
        "owner": dict(OWNER),
        "observed_at": {
            "clock": "python.monotonic_ns",
            "started_ns": 980000000100,
            "completed_ns": 980000000200,
        },
        "memory": {
            "scope": "self_cgroup_and_vm",
            "ancestor_visibility": "not_observed",
            "cgroup_identity_sha256": CGROUP,
            "cgroup_current_bytes": 300,
            "cgroup_max_bytes": 1000,
            "memory_events": {"low": 10, "high": 2, "max": 1, "oom": 0, "oom_kill": 0},
            "vm_total_bytes": 8192,
            "vm_available_bytes": 900,
        },
    }


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def parse_pressure(value, **updates):
    expected = dict(
        expected_capacity_sha256=CAPACITY,
        expected_owner=OWNER,
        expected_cgroup_identity_sha256=CGROUP,
        expected_cgroup_max_bytes=1000,
    )
    expected.update(updates)
    return parse_mineru_process_pressure(encoded(value), **expected)


class ThinPressureConsumerTests(unittest.TestCase):
    def test_independent_wire_roundtrip_preserves_values_and_observed_scope(self):
        value = pressure_payload()
        parsed = parse_pressure(value)
        self.assertEqual(parsed.model_dump(by_alias=True), value)
        self.assertEqual(parsed.memory.observed_headroom_bytes, 700)
        self.assertEqual(parsed.memory.ancestor_visibility, "not_observed")
        self.assertEqual(parsed.observed_at.started_ns, 980000000100)
        value["memory"]["cgroup_current_bytes"] = 1001
        self.assertEqual(parse_pressure(value).memory.observed_headroom_bytes, 0)
        value["memory"]["cgroup_current_bytes"] = 300
        value["memory"]["vm_available_bytes"] = 120
        self.assertEqual(parse_pressure(value).memory.observed_headroom_bytes, 120)

    def test_local_maximum_none_is_exact_qualification_and_missing_is_invalid(self):
        value = pressure_payload()
        value["memory"]["cgroup_max_bytes"] = None
        parsed = parse_pressure(value, expected_cgroup_max_bytes=None)
        self.assertIsNone(parsed.memory.cgroup_max_bytes)
        self.assertEqual(parsed.memory.observed_headroom_bytes, 900)
        self.assertEqual(parsed.memory.ancestor_visibility, "not_observed")
        with self.assertRaises(ValueError):
            parse_pressure(value)
        del value["memory"]["cgroup_max_bytes"]
        with self.assertRaises(ValueError):
            parse_pressure(value, expected_cgroup_max_bytes=None)

    def test_each_external_owner_config_cgroup_or_limit_identity_must_match(self):
        changes = [
            ("capacity_config_sha256", "sha256:" + "9" * 64),
            ("memory.cgroup_identity_sha256", "sha256:" + "8" * 64),
            ("memory.cgroup_max_bytes", 2000),
            ("owner.process_id", 42),
            ("owner.process_start_ticks", 9302),
            ("owner.boot_id", "00000000-0000-4000-8000-000000000005"),
            ("owner.loop_epoch", "00000000-0000-4000-8000-000000000006"),
        ]
        for path, changed in changes:
            value = pressure_payload()
            target = value
            parts = path.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = changed
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_pressure(value)

    def test_closed_objects_reject_unknown_or_missing_fields(self):
        for section in (None, "owner", "observed_at", "memory"):
            value = pressure_payload()
            target = value if section is None else value[section]
            target["extra"] = "no silent schema extension"
            with (
                self.subTest(section=section, mode="extra"),
                self.assertRaises(ValueError),
            ):
                parse_pressure(value)
            value = pressure_payload()
            target = value if section is None else value[section]
            for field in tuple(target):
                copy = deepcopy(value)
                del (copy if section is None else copy[section])[field]
                with (
                    self.subTest(section=section, field=field, mode="missing"),
                    self.assertRaises(ValueError),
                ):
                    parse_pressure(copy)

    def test_strict_numeric_types_counts_and_bracket_never_coerce(self):
        paths = (
            ("memory", "cgroup_current_bytes"),
            ("memory", "vm_available_bytes"),
            ("observed_at", "started_ns"),
            ("owner", "process_id"),
        )
        for section, field in paths:
            for invalid in (True, "1", 1.0, -1, None):
                value = pressure_payload()
                value[section][field] = invalid
                with (
                    self.subTest(section=section, field=field, invalid=invalid),
                    self.assertRaises(ValueError),
                ):
                    parse_pressure(value)
        for invalid in (True, "2", 2.0, -1, 2**63):
            value = pressure_payload()
            value["memory"]["memory_events"]["high"] = invalid
            with self.subTest(event_value=invalid), self.assertRaises(ValueError):
                parse_pressure(value)
        value = pressure_payload()
        value["observed_at"]["completed_ns"] = value["observed_at"]["started_ns"] - 1
        with self.assertRaises(ValueError):
            parse_pressure(value)
        value = pressure_payload()
        value["memory"]["vm_available_bytes"] = 8193
        with self.assertRaises(ValueError):
            parse_pressure(value)

    def test_scope_event_completeness_and_extensible_kernel_counters(self):
        value = pressure_payload()
        value["memory"]["memory_events"]["oom_group_kill"] = 0
        parsed = parse_pressure(value)
        self.assertEqual(parsed.memory.memory_events["oom_group_kill"], 0)
        for key, altered in (
            ("scope", "host_parent_cgroup"),
            ("ancestor_visibility", "known"),
        ):
            value = pressure_payload()
            value["memory"][key] = altered
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_pressure(value)
        for key in ("high", "oom", "oom_kill"):
            value = pressure_payload()
            del value["memory"]["memory_events"][key]
            with self.subTest(event=key), self.assertRaises(ValueError):
                parse_pressure(value)

    def test_raw_json_duplicate_nonfinite_size_and_syntax_fail_closed(self):
        raw = encoded(pressure_payload())
        invalids = (
            b"",
            b" " * 65537,
            b"[]",
            raw[:-1],
            raw.replace(b'"process_id":41', b'"process_id":41,"process_id":41'),
            raw.replace(b'"high":2', b'"high":2,"high":2'),
            raw.replace(b'"vm_available_bytes":900', b'"vm_available_bytes":NaN'),
            raw.replace(b'"vm_available_bytes":900', b'"vm_available_bytes":Infinity'),
        )
        for index, invalid in enumerate(invalids):
            with self.subTest(index=index), self.assertRaises(ValueError):
                parse_mineru_process_pressure(
                    invalid,
                    expected_capacity_sha256=CAPACITY,
                    expected_owner=OWNER,
                    expected_cgroup_identity_sha256=CGROUP,
                    expected_cgroup_max_bytes=1000,
                )
