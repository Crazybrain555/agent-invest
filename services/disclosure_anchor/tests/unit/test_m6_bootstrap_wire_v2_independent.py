"""Independent private bind-v2 oracle; no files, host or business admission.

Expected fields, byte caps and mutations come from R20's protocol boundary.
Existing synthetic spec construction is reused, never a production bind factory
or decoder to manufacture the expected request. Native persistence/ACL and
authentication are verified separately on Windows, not claimed by these tests.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.m6_owner import M6BindOwner, M6OwnerRequest
from disclosure_anchor.application.contracts.m6_run import M6RunSpec

from tests import m6_owner_support as owner_support
from tests import m6_support as m6


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class BindV2WireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = m6.make_fixture("service_diagnostic", {"a": (7, "replay")}).spec
        self.anchor = owner_support.anchor_for(self.spec)
        self.raw = self.spec.canonical_bytes()

    def request(self, raw: bytes, **overrides: object) -> bytes:
        value: dict[str, object] = {
            "contract_version": "m6.owner-request.v2",
            "run_id": self.spec.run_id,
            "spec_sha256": digest(raw),
            "request_id": "independent-bind",
            "command": {
                "kind": "bind", "anchor_sha256": self.anchor.canonical_sha256(),
                "spec_utf8": raw.decode("utf-8"),
            },
        }
        value.update(overrides)
        return canonical(value)

    def spec_with_ids(self, ids: list[str]) -> bytes:
        value = json.loads(self.raw)
        value["carry_in_attempt_ids"] = sorted(ids)
        value["resources"]["max_attempts"] = max(20, len(ids))
        value["resources"]["max_events"] = max(200, len(ids))
        raw = canonical(value)
        # Only checks that a proposed boundary input is otherwise a legal spec;
        # this does not use the new bind validator or a measured expected cap.
        M6RunSpec.from_canonical_bytes(raw, maximum_bytes=1_048_576)
        return raw

    def decode(self, wire: bytes) -> M6OwnerRequest:
        return M6OwnerRequest.from_canonical_bytes(wire, maximum_bytes=65536)

    def test_exact_spec_bytes_and_hash_survive_complete_v2_round_trip(self) -> None:
        wire = self.request(self.raw)
        request = self.decode(wire)
        self.assertEqual(request.canonical_bytes(), wire)
        self.assertEqual(request.contract_version, "m6.owner-request.v2")
        self.assertIsInstance(request.command, M6BindOwner)
        self.assertEqual(request.command.spec_utf8.encode("utf-8"), self.raw)  # type: ignore[union-attr]
        self.assertEqual(request.spec_sha256, digest(self.raw))
        self.assertEqual(set(json.loads(wire)["command"]), {"kind", "anchor_sha256", "spec_utf8"})

    def test_old_missing_spec_shape_and_unknown_fields_are_refused(self) -> None:
        original = json.loads(self.request(self.raw))
        variants = {
            "old_version": {**original, "contract_version": "m6.owner-request.v1"},
            "missing_spec": {**original, "command": {k: v for k, v in original["command"].items() if k != "spec_utf8"}},
            "path_instead": {**original, "command": {"kind": "bind", "anchor_sha256": self.anchor.canonical_sha256(), "path": "spec.json"}},
            "extra_field": {**original, "command": {**original["command"], "extra": True}},
            "nonstring_spec": {**original, "command": {**original["command"], "spec_utf8": json.loads(self.raw)}},
        }
        for label, value in variants.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.decode(canonical(value))

    def test_hash_and_run_identity_cannot_disagree_with_payload(self) -> None:
        for values in ({"spec_sha256": m6.digest("other-spec")}, {"run_id": "other-run"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.decode(self.request(self.raw, **values))

    def test_noncanonical_duplicate_and_semantically_invalid_specs_are_refused(self) -> None:
        value = json.loads(self.raw)
        variants = {
            "spacing": json.dumps(value, sort_keys=True).encode(),
            "duplicate_key": self.raw[:-1] + b',"run_id":' + canonical(self.spec.run_id) + b'}',
            "unknown_field": canonical({**value, "extra": 1}),
            "float_tick": canonical({**value, "t0_ticks": float(value["t0_ticks"])}),
            "control_character": canonical({**value, "run_id": "bad\nidentifier"}),
            "wrong_original_interval": canonical({**value, "deadline_ticks": value["deadline_ticks"] + 1}),
        }
        for label, raw in variants.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.decode(self.request(raw))

    def test_payload_cap_counts_utf8_bytes_not_unicode_characters(self) -> None:
        raw = self.spec_with_ids([f"{i:04d}" + "😀" * 120 for i in range(110)])
        self.assertLess(len(raw.decode("utf-8")), 49152)
        self.assertGreater(len(raw), 49152)
        with self.assertRaises(ValueError):
            M6BindOwner(anchor_sha256=self.anchor.canonical_sha256(), spec_utf8=raw.decode("utf-8"))

    def test_escaped_whole_wire_limit_is_separate_from_payload_limit(self) -> None:
        raw = self.spec_with_ids([f"{i:04d}" + '"' * 120 for i in range(150)])
        wire = self.request(raw)
        self.assertLess(len(raw), 49152)
        self.assertGreater(len(wire), 65536)
        # A payload that is small enough is legal on its own; its enclosing
        # escaped request is too large and must never reach the transport.
        command = M6BindOwner(anchor_sha256=self.anchor.canonical_sha256(), spec_utf8=raw.decode("utf-8"))
        self.assertEqual(command.spec_utf8.encode("utf-8"), raw)
        with self.assertRaises(ValueError):
            self.decode(wire)
        spec = M6RunSpec.from_canonical_bytes(raw, maximum_bytes=49152)
        owner = owner_support.ScriptedOwner(spec, owner_support.anchor_for(spec), owner_support.ManualClock(owner_support.NS))
        client = owner_support.client_for(owner, role="controller", epoch=m6.digest("controller"))
        with self.assertRaises(ValueError):
            client.bind()
        self.assertEqual(owner.requests, [], "oversized escaped bind must be refused before any exchange")


if __name__ == "__main__":
    unittest.main()
