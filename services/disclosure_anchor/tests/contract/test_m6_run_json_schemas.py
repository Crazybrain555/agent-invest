"""Independently authored checks of the generated private M6 operational schemas.

The exporter under test is `operational_m6_schema_documents`. Its output is
never compared with itself: acceptance/rejection is checked with hand-built
instances under an independent JSON Schema validator, structural closure is
asserted directly on the documents, and any on-disk export found in the
repository must equal the live generator (drift detection). Public v1 schema
bytes are outside this module's scope and are not touched.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import disclosure_anchor
from disclosure_anchor.cli import export_contracts
from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope
from disclosure_anchor.application.contracts.m6_document_qualification import qualify_document
from disclosure_anchor.application.contracts.m6_owner import (
    M6BindOwner, M6OwnerAnchor, M6OwnerReply, M6OwnerRequest, M6OwnerStatus,
)
from disclosure_anchor.application.contracts.m6_schemas import operational_m6_schema_documents

from tests import m6_support as m6

import jsonschema


EXPECTED_FILES = {
    "m6-campaign-scope.v1.schema.json": "m6.campaign-scope.v1",
    "m6-corpus-manifest.v1.schema.json": "m6.corpus-manifest.v1",
    "m6-quality-plan.v1.schema.json": "m6.quality-plan.v1",
    "m6-qualification-evidence.v1.schema.json": "m6.qualification-evidence.v1",
    "m6-document-qualification.v1.schema.json": "m6.document-qualification.v1",
    "m6-source-history-fact.v1.schema.json": None,
    "m6-run-spec.v1.schema.json": "m6.run-spec.v1",
    "m6-producer-event.v1.schema.json": "m6.producer-event.v1",
    "m6-run-event.v1.schema.json": "m6.run-event.v1",
    "m6-run-receipt.v1.schema.json": "m6.run-receipt.v1",
    "m6-owner-anchor.v1.schema.json": "m6.owner-anchor.v1",
    "m6-owner-request.v1.schema.json": "m6.owner-request.v1",
    "m6-owner-status.v1.schema.json": "m6.owner-status.v1",
    "m6-owner-reply.v1.schema.json": "m6.owner-reply.v1",
}
DRAFT = "https://json-schema.org/draft/2020-12/schema"
ID_PREFIX = "urn:disclosure-anchor:operational:"


def _object_schemas(document: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    found: list[tuple[str, dict[str, object]]] = [("<root>", document)]
    for name, definition in dict(document.get("$defs", {})).items():  # type: ignore[call-overload]
        found.append((name, definition))
    return [(name, schema) for name, schema in found if schema.get("type") == "object" or "properties" in schema]


def _literal_values(schema: dict[str, object]) -> set[object]:
    if "const" in schema:
        return {schema["const"]}
    if "enum" in schema:
        return set(schema["enum"])  # type: ignore[arg-type]
    return set()


def _repo_root() -> Path:
    package = Path(disclosure_anchor.__file__).resolve().parent
    return package.parent.parent  # src/disclosure_anchor -> repository/service root


class ClosedSchemaExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = operational_m6_schema_documents()

    def test_closed_schemas_are_valid_exact_exports_and_in_complete_registry(self) -> None:
        self.assertEqual(set(self.documents), set(EXPECTED_FILES), "the private registry is exactly these fourteen")
        for filename, document in self.documents.items():
            self.assertEqual(document["$schema"], DRAFT, filename)
            self.assertEqual(document["$id"], ID_PREFIX + filename, filename)
            self.assertTrue(filename.startswith("m6-") and filename.endswith(".v1.schema.json"), filename)
            json.dumps(document, sort_keys=True)  # serialisable without custom encoders
            expected_version = EXPECTED_FILES[filename]
            version_schema = dict(document.get("properties", {})).get("contract_version")  # type: ignore[call-overload]
            if expected_version is None:
                self.assertIsNone(version_schema, filename + " carries no contract_version by design")
            else:
                self.assertIsNotNone(version_schema, filename)
                self.assertEqual(_literal_values(version_schema), {expected_version}, filename)  # type: ignore[arg-type]
            for name, schema in _object_schemas(document):
                self.assertIs(schema.get("additionalProperties"), False, filename + "#" + name + " must be closed")
                properties = dict(schema.get("properties", {}))  # type: ignore[call-overload]
                required = set(schema.get("required", []))  # type: ignore[arg-type]
                defaulted = {key for key, value in properties.items() if isinstance(value, dict) and "default" in value}
                self.assertEqual(required | defaulted, set(properties),
                                 filename + "#" + name + ": every field is required unless it has a frozen default")
                self.assertTrue(required.isdisjoint(defaulted), filename + "#" + name)
        self.assertNotIn("public", " ".join(self.documents), "no public v1 schema is emitted by this exporter")

        # The generator is a pure function of the models: repeated calls agree byte for byte.
        again = operational_m6_schema_documents()
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.documents, sort_keys=True))

    def test_export_registry_routes_through_the_m6_generator(self) -> None:
        registry = export_contracts.operational_schema_documents()
        registered_m6 = {name: schema for name, schema in registry.items() if name.startswith("m6-")}
        self.assertEqual(registered_m6, self.documents,
                         "the existing operational registry must include every M6 schema")
        # Exercise the real exporter, only into disposable local storage. This
        # checks its observable output even when registry routing is indirect.
        with tempfile.TemporaryDirectory(prefix="m6-schema-export-") as directory:
            root = Path(directory)
            written = export_contracts.export_contracts(root)
            written_m6 = {path.name for path in written if path.name.startswith("m6-")}
            self.assertEqual(written_m6, set(EXPECTED_FILES))
            for name in EXPECTED_FILES:
                self.assertEqual(json.loads((root / "operational" / name).read_bytes()), self.documents[name])

    def test_on_disk_exports_match_the_live_generator(self) -> None:
        root = _repo_root() / "contracts" / "operational"
        present = {path.name for path in root.glob("m6-*.v1.schema.json")}
        self.assertEqual(present, set(EXPECTED_FILES), "canonical operational exports must be complete")
        for name in EXPECTED_FILES:
            path = root / name
            self.assertEqual(json.loads(path.read_bytes()), self.documents[name],
                             str(path) + " differs from the live generator")


class SchemaAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = operational_m6_schema_documents()
        self.fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh"), "b": (3, "replay")})
        self.journal = m6.Journal(self.fixture)
        self.journal.start()
        self.journal.opened(self.journal.at(1))
        self.journal.close_run(stop_at=60)
        self.receipt = m6.reduce(self.fixture, self.journal)

    def validator(self, filename: str) -> object:
        cls = jsonschema.validators.validator_for(self.documents[filename])  # type: ignore[union-attr]
        cls.check_schema(self.documents[filename])
        return cls(self.documents[filename])

    def assert_accepts(self, filename: str, instance: object) -> None:
        errors = list(self.validator(filename).iter_errors(instance))  # type: ignore[attr-defined]
        self.assertEqual(errors, [], filename + " should accept a canonical instance: " + "; ".join(e.message for e in errors))

    def assert_rejects(self, filename: str, instance: object, label: str) -> None:
        errors = list(self.validator(filename).iter_errors(instance))  # type: ignore[attr-defined]
        self.assertTrue(errors, filename + " should reject " + label)

    def test_representative_instances_are_accepted_and_adjacent_mutations_rejected(self) -> None:
        spec, corpus, plan = self.fixture.spec, self.fixture.manifest, self.fixture.plan
        scope = M6CampaignScope.from_manifest(corpus)
        proof = m6.qualification_for(self.fixture.entries["a"], "e2e_publication", "att-a")
        qualification = qualify_document(proof, plan)
        record = self.journal.records[0]
        anchor = M6OwnerAnchor(
            run_id=spec.run_id, clock=spec.clock, owner_process_epoch_sha256=m6.OWNER_EPOCH,
            owner_source_sha256=spec.runtime.owner_source_sha256,
            gpu_device_identity_sha256=spec.runtime.gpu_device_identity_sha256, t0_ticks=spec.t0_ticks,
            planned_seconds=spec.planned_seconds, deadline_ticks=spec.deadline_ticks,
            max_close_ticks=spec.max_close_ticks, resources=spec.resources)
        request = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec.canonical_sha256(), request_id="req-1",
                                 command=M6BindOwner(anchor_sha256=anchor.canonical_sha256()))
        status = M6OwnerStatus(run_id=spec.run_id, spec_sha256=spec.canonical_sha256(),
                               anchor_sha256=anchor.canonical_sha256(), owner_process_epoch_sha256=m6.OWNER_EPOCH,
                               observed_qpc_ticks=self.fixture.at(10), state="open", last_sequence=2,
                               admission_valid_until_ticks=self.fixture.at(11))
        reply = M6OwnerReply(request_sha256=request.canonical_sha256(), outcome="ok", status=status,
                             record=record, error_code=None)
        history = m6.history_fact(self.fixture.entries["a"], attempt_id="att-a", ledger_seq=1)
        instances = {
            "m6-campaign-scope.v1.schema.json": scope,
            "m6-corpus-manifest.v1.schema.json": corpus,
            "m6-quality-plan.v1.schema.json": plan,
            "m6-qualification-evidence.v1.schema.json": proof,
            "m6-document-qualification.v1.schema.json": qualification,
            "m6-source-history-fact.v1.schema.json": history,
            "m6-run-spec.v1.schema.json": spec,
            "m6-producer-event.v1.schema.json": record.event,
            "m6-run-event.v1.schema.json": record,
            "m6-run-receipt.v1.schema.json": self.receipt,
            "m6-owner-anchor.v1.schema.json": anchor,
            "m6-owner-request.v1.schema.json": request,
            "m6-owner-status.v1.schema.json": status,
            "m6-owner-reply.v1.schema.json": reply,
        }
        self.assertEqual(set(instances), set(self.documents))
        for filename, model in instances.items():
            wire = json.loads(model.canonical_bytes())
            self.assert_accepts(filename, wire)
            self.assert_rejects(filename, {**wire, "unexpected": 1}, "an unknown top-level field")
            required_key = next(key for key in wire if key not in {"contract_version", "kind"})
            self.assert_rejects(filename, {k: v for k, v in wire.items() if k != required_key},
                                "a missing required field (" + required_key + ")")
            self.assert_rejects(filename, [], "a non-object")
            if "contract_version" in wire:
                self.assert_rejects(filename, {**wire, "contract_version": "m6.other.v9"}, "another contract version")

        # Hash-shaped fields carry a pattern; ids and counts carry their bounds.
        spec_wire = json.loads(spec.canonical_bytes())
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "manifest_sha256": "sha256:" + "G" * 64}, "non-hex hash")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "manifest_sha256": spec_wire["manifest_sha256"][:-1]}, "short hash")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "planned_seconds": 0}, "zero planned seconds")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "planned_seconds": "60"}, "string planned seconds")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "run_id": "has space"}, "id with whitespace")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "phase": "marathon"}, "unknown phase")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "carry_in_attempt_ids": "x"}, "non-array carry-in")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "resources": {**spec_wire["resources"], "max_events": 0}},
                            "nested bound violation")
        self.assert_rejects("m6-run-spec.v1.schema.json", {**spec_wire, "resources": {**spec_wire["resources"], "extra": 1}},
                            "nested unknown field")
        manifest_wire = json.loads(corpus.canonical_bytes())
        self.assert_rejects("m6-corpus-manifest.v1.schema.json", {**manifest_wire, "entries": []}, "empty membership")
        self.assert_rejects("m6-corpus-manifest.v1.schema.json", {**manifest_wire, "mode": "benchmark"}, "unknown mode")
        self.assert_rejects("m6-corpus-manifest.v1.schema.json", {**manifest_wire, "entries": [
            {**manifest_wire["entries"][0], "origin": "synthetic"}]}, "unknown origin")
        receipt_wire = json.loads(self.receipt.canonical_bytes())
        self.assert_rejects("m6-run-receipt.v1.schema.json", {**receipt_wire, "status": "green"}, "unknown status")
        self.assert_rejects("m6-run-receipt.v1.schema.json", {**receipt_wire, "metrics": {"kind": "pages_per_hour", "value": 1}},
                            "an unregistered metrics kind")
        self.assert_rejects("m6-run-receipt.v1.schema.json", {**receipt_wire, "events_total": -1}, "negative count")
        event_wire = json.loads(record.event.canonical_bytes())
        self.assert_rejects("m6-producer-event.v1.schema.json", {**event_wire, "payload": {"kind": "run_started"}},
                            "a payload missing its fields")
        self.assert_rejects("m6-producer-event.v1.schema.json", {**event_wire, "payload": {**event_wire["payload"], "kind": "unknown"}},
                            "an unregistered event kind")
        self.assert_rejects("m6-producer-event.v1.schema.json", {**event_wire, "producer_kind": "anyone"}, "unknown producer")
        self.assert_rejects("m6-producer-event.v1.schema.json", {**event_wire, "producer_sequence": 0}, "zero producer sequence")
        reply_wire = json.loads(reply.canonical_bytes())
        self.assert_rejects("m6-owner-reply.v1.schema.json", {**reply_wire, "outcome": "maybe"}, "unknown outcome")
        self.assert_rejects("m6-owner-reply.v1.schema.json", {**reply_wire, "record": {**reply_wire["record"], "stamp": {}}},
                            "a stamp without fields")
        request_wire = json.loads(request.canonical_bytes())
        self.assert_rejects("m6-owner-request.v1.schema.json", {**request_wire, "command": {"kind": "exec", "argv": ["x"]}},
                            "an arbitrary command; the control protocol is closed")
        self.assert_rejects("m6-owner-request.v1.schema.json", {**request_wire, "command": {"kind": "status", "token": "x"}},
                            "credentials inside a durable control model")
        self.assert_accepts("m6-owner-request.v1.schema.json", {**request_wire, "command": {"kind": "status"}})

        # Service-mode instances are accepted by the same schemas (mode is a closed enum, not a separate document).
        service = m6.make_fixture("service_diagnostic", {"s": (2, "fresh")})
        self.assert_accepts("m6-run-spec.v1.schema.json", json.loads(service.spec.canonical_bytes()))
        self.assert_accepts("m6-corpus-manifest.v1.schema.json", json.loads(service.manifest.canonical_bytes()))
        self.assert_accepts("m6-quality-plan.v1.schema.json", json.loads(service.plan.canonical_bytes()))


if __name__ == "__main__":
    unittest.main()
