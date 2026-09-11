"""Independent checks that the M6 owner wire fixture generator is self-consistent.

The generated vectors are the cross-language oracle for the native C# suite.
These tests prove the Python side of that oracle: every vector round-trips
through the closed production models, hashes bind exact canonical bytes, the
Unicode ordering premise really differs between scalar and UTF-16 order, and
the committed JSON is byte-identical to a fresh generation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from disclosure_anchor.application.contracts.m6_owner import M6OwnerAnchor, M6OwnerReply, M6OwnerRequest
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.m6_run_events import M6RunEvent

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "m6_owner"


def _load_generator():  # type: ignore[no-untyped-def]
    import importlib.util

    path = FIXTURE_DIR / "generate_m6_owner_fixtures.py"
    spec = importlib.util.spec_from_file_location("m6_owner_fixture_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generator = _load_generator()
        cls.document = cls.generator.generate()

    def test_committed_vectors_are_byte_identical_to_generation(self) -> None:
        committed = FIXTURE_DIR / "wire-vectors.v2.json"
        self.assertTrue(committed.is_file(), "committed fixture missing: " + str(committed))
        self.assertEqual(committed.read_text(encoding="utf-8"), self.generator.render())

    def test_spec_and_anchor_round_trip_closed_models(self) -> None:
        for mode in ("service", "e2e"):
            block = self.document[mode]
            spec = M6RunSpec.from_canonical_bytes(block["spec"].encode(), maximum_bytes=65536)
            anchor = M6OwnerAnchor.from_canonical_bytes(block["anchor"].encode(), maximum_bytes=65536)
            anchor.assert_spec(spec)
            self.assertEqual(spec.canonical_bytes().decode(), block["spec"])
        self.assertEqual(
            M6RunSpec.from_canonical_bytes(self.document["service"]["spec"].encode(), maximum_bytes=65536).canonical_sha256(),
            self.document["identities"]["spec_sha256"],
        )

    def test_unicode_carry_in_premise(self) -> None:
        ids = self.document["service"]["carry_in_scalar_order"]
        self.assertEqual(ids, sorted(ids))
        utf16 = sorted(ids, key=lambda s: s.encode("utf-16-be"))
        self.assertNotEqual(ids, utf16, "fixture requires scalar order to differ from UTF-16 ordinal order")
        utf8 = sorted(ids, key=lambda s: s.encode("utf-8"))
        self.assertEqual(ids, utf8, "scalar order must equal UTF-8 byte order")

    def test_every_binding_negative_is_rejected_by_python(self) -> None:
        names = set()
        for item in self.document["binding_negatives"]:
            names.add(item["name"])
            with self.assertRaises((ValueError, TypeError), msg=item["name"]):
                spec = M6RunSpec.from_canonical_bytes(item["spec"].encode(), maximum_bytes=65536)
                M6OwnerAnchor.from_canonical_bytes(item["anchor"].encode(), maximum_bytes=65536).assert_spec(spec)
        self.assertEqual(len(names), len(self.document["binding_negatives"]))

    def test_journal_records_bind_stamps_and_hashes(self) -> None:
        journal = self.document["journal"]
        previous_sequence = 0
        for raw, sha, producer_raw, producer_sha in zip(
            journal["records"], journal["record_hashes"], journal["producer_events"], journal["producer_hashes"],
        ):
            record = M6RunEvent.from_canonical_bytes(raw.encode(), maximum_bytes=16384)
            self.assertEqual("sha256:" + hashlib.sha256(raw.encode()).hexdigest(), sha)
            self.assertEqual(record.event.canonical_bytes().decode(), producer_raw)
            self.assertEqual(record.event.canonical_sha256(), producer_sha)
            self.assertEqual(record.stamp.producer_event_sha256, producer_sha)
            self.assertEqual(record.stamp.sequence, previous_sequence + 1)
            previous_sequence = record.stamp.sequence
        resumed = M6RunEvent.from_canonical_bytes(journal["resumed_record"].encode(), maximum_bytes=16384)
        self.assertEqual(resumed.event.payload.kind, "owner_resumed")
        self.assertEqual(resumed.stamp.owner_process_epoch_sha256, self.document["identities"]["owner_epoch_2"])
        self.assertEqual(resumed.event.payload.previous_owner_epoch_sha256, self.document["identities"]["owner_epoch"])

    def test_journal_negatives_are_distinct_from_positives(self) -> None:
        positives = set(self.document["journal"]["records"])
        for item in self.document["journal"]["negatives"]:
            self.assertNotIn(item["record"], positives, item["name"])
            self.assertIn(item["after_prefix"], range(0, len(positives)))

    def test_requests_and_reply_round_trip(self) -> None:
        requests = self.document["requests"]
        for key in ("bind", "append", "status", "drain_a", "drain_b", "admission_closed", "close"):
            request = M6OwnerRequest.from_canonical_bytes(requests[key].encode(), maximum_bytes=65536)
            self.assertEqual(request.canonical_bytes().decode(), requests[key])
        self.assertEqual(
            M6OwnerRequest.from_canonical_bytes(requests["append"].encode(), maximum_bytes=65536).canonical_sha256(),
            requests["append_sha256"],
        )
        with self.assertRaises(ValueError):
            M6OwnerRequest.from_canonical_bytes(requests["cross_run_append_rejected"].encode(), maximum_bytes=65536)
        reply = M6OwnerReply.from_canonical_bytes(self.document["reply"]["canonical"].encode(), maximum_bytes=65536)
        self.assertEqual(reply.request_sha256, requests["append_sha256"])
        self.assertEqual(reply.canonical_sha256(), self.document["reply"]["sha256"])

    def test_pending_drain_identity_excludes_request_nonce(self) -> None:
        requests = self.document["requests"]
        a = json.loads(requests["drain_a"])
        b = json.loads(requests["drain_b"])
        self.assertNotEqual(a["request_id"], b["request_id"])
        self.assertEqual(a["command"], b["command"])
        identity = json.loads(self.document["pending_drain"]["identity_raw"])
        self.assertEqual(set(identity), {"command", "run_id", "spec_sha256"})
        self.assertEqual(identity["command"], a["command"])
        chain = self.document["pending_drain"]
        expected = "sha256:" + hashlib.sha256(
            (chain["request_a_sha256"] + "\n" + chain["request_b_sha256"]).encode()
        ).hexdigest()
        self.assertEqual(chain["chain_after_two"], expected)

    def test_attempt_set_hash_is_order_independent(self) -> None:
        ids = self.document["attempt_set"]["ids"]
        lines = sorted("sha256:" + hashlib.sha256(i.encode()).hexdigest() for i in ids)
        expected = "sha256:" + hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()
        self.assertEqual(self.document["attempt_set"]["sha256"], expected)
        reversed_lines = sorted("sha256:" + hashlib.sha256(i.encode()).hexdigest() for i in reversed(ids))
        self.assertEqual(lines, reversed_lines)

    def test_quote_cases_match_python_json(self) -> None:
        for case in self.document["quote_cases"]:
            self.assertEqual(json.loads(case["quoted"]), case["value"])
            self.assertEqual(case["quoted"], json.dumps(case["value"], ensure_ascii=False))

    def test_no_real_identity_or_credential_material(self) -> None:
        text = json.dumps(self.document)
        for forbidden in ("C:\\ProgramData", "GPU-", "M6-AUTH", "MachineGuid", "BootId"):
            self.assertNotIn(forbidden, text)
        for value in self.document["identities"].values():
            self.assertRegex(value, r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
