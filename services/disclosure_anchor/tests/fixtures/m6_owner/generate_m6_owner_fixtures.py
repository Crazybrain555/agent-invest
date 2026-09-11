"""Independent M6 owner cross-language wire fixture generator.

Authored independently from the native C# implementation. It imports only the
closed production Python contract models (never previous tests, m6_support or
old fixture files) and emits canonical byte vectors plus expected outcomes the
native suite must reproduce exactly. Every hash is computed from Python canonical
bytes; the native side must agree byte-for-byte or the vector fails.

Usage (from the repository root, with the service virtualenv):

    PYTHONPATH=src python tests/fixtures/m6_owner/generate_m6_owner_fixtures.py \
        --output tests/fixtures/m6_owner/wire-vectors.v2.json

    PYTHONPATH=src python tests/fixtures/m6_owner/generate_m6_owner_fixtures.py --check

Synthetic placeholders only: no production PDF, PostgreSQL, credentials or
real Windows identity values appear here. Token-like values are deterministic
test-only strings and must never be deployed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from disclosure_anchor.application.contracts.m6_owner import (
    M6AdmissionClosedAck, M6AppendObservation, M6BindOwner, M6CloseOwner, M6OwnerAnchor, M6OwnerControl,
    M6OwnerReply, M6OwnerRequest, M6OwnerStatus,
)
from disclosure_anchor.application.contracts.m6_run import (
    M6ClockDomain, M6ResourceEnvelope, M6RunSpec, M6RuntimeIdentity,
)
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AdmissionControl, M6AttemptAdmitted, M6AttemptFinal, M6OwnerResumed, M6OwnerStamp, M6ProducerEvent,
    M6RunEvent, M6RunStarted, M6VerifierDrained,
)

FIXTURE_VERSION = "m6.owner-wire-vectors.v2"
QPC_FREQUENCY = 10_000_000
CONTROL_SAMPLE = "ctl" + chr(1) + chr(31)


def sha_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def label_hash(label: str) -> str:
    """Deterministic synthetic sha256 placeholder derived from a readable label."""
    return sha_text("m6-independent-fixture|" + label)


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def clock_domain(boot_label: str) -> M6ClockDomain:
    boot = label_hash(boot_label)
    domain = {"boot_identity_sha256": boot, "clock_source": "QueryPerformanceCounter", "frequency_hz": QPC_FREQUENCY}
    return M6ClockDomain(
        host_assignment_identity_sha256=label_hash("host-assignment"),
        boot_identity_sha256=boot,
        qpc_frequency_hz=QPC_FREQUENCY,
        clock_domain_identity_sha256=sha_text(canonical(domain)),
    )


def resources() -> M6ResourceEnvelope:
    return M6ResourceEnvelope(
        max_events=1000, max_record_bytes=16384, max_log_bytes=1_000_000, max_attempts=100,
        max_verifier_backlog_bytes=1_048_576, stop_admission_budget_ticks=30 * QPC_FREQUENCY,
    )


def runtime(mode: str) -> M6RuntimeIdentity:
    return M6RuntimeIdentity(
        source_commit="0123456789abcdef0123456789abcdef01234567",
        source_manifest_sha256=label_hash("source-manifest"),
        runtime_bundle_identity_sha256=label_hash("runtime-bundle"),
        process_profile_sha256=label_hash("process-profile"),
        worker_profile_sha256=label_hash("worker-profile") if mode == "e2e_publication" else None,
        owner_source_sha256=label_hash("owner-source"),
        gpu_device_identity_sha256=label_hash("gpu-device"),
        deployment_qualification_sha256=label_hash("deployment-qualification"),
    )


def build_spec(*, mode: str, run_id: str, t0: int, planned: int, carry_in: tuple[str, ...],
               phase: str = "short_batch") -> M6RunSpec:
    clock = clock_domain("boot-a")
    return M6RunSpec(
        run_id=run_id, campaign_id="campaign-fixture-1", mode=mode, phase=phase, start_condition="cold",
        clock=clock, runtime=runtime(mode), manifest_sha256=label_hash("manifest"),
        scope_sha256=label_hash("scope") if mode == "e2e_publication" else None,
        quality_plan_sha256=label_hash("quality-plan"), t0_ticks=t0, planned_seconds=planned,
        deadline_ticks=t0 + planned * QPC_FREQUENCY, max_close_ticks=t0 + (planned + 60) * QPC_FREQUENCY,
        carry_in_attempt_ids=carry_in, resources=resources(),
    )


def build_anchor(spec: M6RunSpec, owner_epoch: str) -> M6OwnerAnchor:
    return M6OwnerAnchor(
        run_id=spec.run_id, clock=spec.clock, owner_process_epoch_sha256=owner_epoch,
        owner_source_sha256=spec.runtime.owner_source_sha256,
        gpu_device_identity_sha256=spec.runtime.gpu_device_identity_sha256,
        t0_ticks=spec.t0_ticks, planned_seconds=spec.planned_seconds, deadline_ticks=spec.deadline_ticks,
        max_close_ticks=spec.max_close_ticks, resources=spec.resources,
    )


def stamp_record(event: M6ProducerEvent, *, sequence: int, tick: int, boot: str, owner: str) -> M6RunEvent:
    return M6RunEvent(event=event, stamp=M6OwnerStamp(
        sequence=sequence, received_qpc_ticks=tick, boot_identity_sha256=boot,
        owner_process_epoch_sha256=owner, producer_event_sha256=event.canonical_sha256(),
    ))


def _variant(base: dict[str, Any], mutate: Any) -> str:
    copy = json.loads(json.dumps(base))
    mutate(copy)
    return canonical(copy)


def generate() -> dict[str, Any]:
    t0 = 5_000_000_000
    owner_epoch = label_hash("owner-epoch-1")
    owner_epoch_2 = label_hash("owner-epoch-2")
    runner_epoch = label_hash("service-runner-epoch")
    verifier_epoch = label_hash("quality-verifier-epoch")
    # Unicode scalar ordering: U+FF5E (BMP, UTF-16 0xFF5E) sorts BEFORE U+1F600
    # (supplementary, UTF-16 lead surrogate 0xD83D) in scalar/UTF-8 order, but
    # AFTER it in UTF-16 ordinal order. Python's sorted() uses scalar order.
    unicode_ids = tuple(sorted({"a-plain", "～-fullwidth", "\U0001f600-emoji", "z-last"}))
    spec = build_spec(mode="service_diagnostic", run_id="run-fixture-service", t0=t0, planned=30, carry_in=unicode_ids)
    anchor = build_anchor(spec, owner_epoch)
    spec_sha = spec.canonical_sha256()
    boot = spec.clock.boot_identity_sha256

    e2e_spec = build_spec(mode="e2e_publication", run_id="run-fixture-e2e", t0=t0, planned=3600, carry_in=(),
                          phase="hour_baseline")
    e2e_anchor = build_anchor(e2e_spec, owner_epoch)

    spec_json = json.loads(spec.canonical_bytes())
    anchor_json = json.loads(anchor.canonical_bytes())
    anchor_text = anchor.canonical_bytes().decode()

    def set_carry_utf16_order(doc: dict[str, Any]) -> None:
        doc["carry_in_attempt_ids"] = sorted(doc["carry_in_attempt_ids"], key=lambda s: s.encode("utf-16-be"))

    def unsorted_carry(doc: dict[str, Any]) -> None:
        doc["carry_in_attempt_ids"] = list(reversed(doc["carry_in_attempt_ids"]))

    def dup_carry(doc: dict[str, Any]) -> None:
        doc["carry_in_attempt_ids"] = [doc["carry_in_attempt_ids"][0]] * 2

    def wrong_t0(doc: dict[str, Any]) -> None:
        doc["t0_ticks"] += 1
        doc["deadline_ticks"] += 1

    def scope_in_service(doc: dict[str, Any]) -> None:
        doc["scope_sha256"] = label_hash("scope")

    def short_hour(doc: dict[str, Any]) -> None:
        doc["phase"] = "hour_baseline"

    def extra_key(doc: dict[str, Any]) -> None:
        doc["extra"] = 1

    def bad_commit(doc: dict[str, Any]) -> None:
        doc["runtime"]["source_commit"] = "0123456789ABCDEF0123456789abcdef01234567"

    def owner_source_drift(doc: dict[str, Any]) -> None:
        doc["runtime"]["owner_source_sha256"] = label_hash("owner-source-other")

    binding_negatives = [
        {"name": "carry_in_utf16_order_rejected", "spec": _variant(spec_json, set_carry_utf16_order), "anchor": anchor_text},
        {"name": "carry_in_unsorted_rejected", "spec": _variant(spec_json, unsorted_carry), "anchor": anchor_text},
        {"name": "carry_in_duplicate_rejected", "spec": _variant(spec_json, dup_carry), "anchor": anchor_text},
        {"name": "t0_differs_from_anchor", "spec": _variant(spec_json, wrong_t0), "anchor": anchor_text},
        {"name": "service_mode_with_scope", "spec": _variant(spec_json, scope_in_service), "anchor": anchor_text},
        {"name": "hour_phase_below_3600", "spec": _variant(spec_json, short_hour), "anchor": anchor_text},
        {"name": "unknown_spec_key", "spec": _variant(spec_json, extra_key), "anchor": anchor_text},
        {"name": "uppercase_source_commit", "spec": _variant(spec_json, bad_commit), "anchor": anchor_text},
        {"name": "owner_source_differs_from_anchor", "spec": _variant(spec_json, owner_source_drift), "anchor": anchor_text},
        {"name": "noncanonical_spec_whitespace", "spec": json.dumps(spec_json, sort_keys=True, indent=1), "anchor": anchor_text},
        {"name": "noncanonical_anchor_key_order", "spec": spec.canonical_bytes().decode(),
         "anchor": json.dumps(dict(reversed(list(anchor_json.items()))), sort_keys=False, separators=(",", ":"), ensure_ascii=False)},
    ]
    # The UTF-16 ordering variant must actually differ from scalar order or the
    # vector proves nothing. Sanity-check the fixture's own premise.
    if json.loads(binding_negatives[0]["spec"])["carry_in_attempt_ids"] == list(unicode_ids):
        raise AssertionError("UTF-16 ordering variant equals scalar ordering; fixture premise broken")
    for item in binding_negatives:
        rejected = False
        try:
            candidate = M6RunSpec.from_canonical_bytes(item["spec"].encode(), maximum_bytes=65536)
            M6OwnerAnchor.from_canonical_bytes(item["anchor"].encode(), maximum_bytes=65536).assert_spec(candidate)
        except (ValueError, TypeError):
            rejected = True
        if not rejected:
            raise AssertionError("binding negative is accepted by Python contract: " + item["name"])

    def owner_event(seq: int, payload: Any) -> M6ProducerEvent:
        return M6ProducerEvent(run_id=spec.run_id, spec_sha256=spec_sha, producer_kind="owner",
                               producer_epoch_sha256=owner_epoch, producer_sequence=seq, payload=payload)

    def runner_event(seq: int, payload: Any) -> M6ProducerEvent:
        return M6ProducerEvent(run_id=spec.run_id, spec_sha256=spec_sha, producer_kind="service_runner",
                               producer_epoch_sha256=runner_epoch, producer_sequence=seq, payload=payload)

    admitted = M6AttemptAdmitted(
        attempt_id="attempt-é-1", fence_identity="fence-1", document_id=None, processing_run_id=None,
        source_pdf_sha256=label_hash("source-pdf-1"), source_byte_count=4096, source_page_count=3,
        process_profile_sha256=spec.runtime.process_profile_sha256,
    )
    final = M6AttemptFinal(attempt_id=admitted.attempt_id, outcome="failed", remote_disposition="not_submitted",
                           remote_receipt_sha256=None, remote_task_identity_sha256=None,
                           cleanup_receipt_sha256=label_hash("cleanup-1"))
    records = [
        stamp_record(owner_event(1, M6RunStarted(clock=spec.clock, t0_ticks=t0, deadline_ticks=spec.deadline_ticks)),
                     sequence=1, tick=t0, boot=boot, owner=owner_epoch),
        stamp_record(owner_event(2, M6AdmissionControl(kind="admission_opened")),
                     sequence=2, tick=t0 + 10, boot=boot, owner=owner_epoch),
        stamp_record(runner_event(1, admitted), sequence=3, tick=t0 + 20, boot=boot, owner=owner_epoch),
        stamp_record(runner_event(2, final), sequence=4, tick=t0 + 30, boot=boot, owner=owner_epoch),
        stamp_record(owner_event(3, M6AdmissionControl(kind="stop_admission_requested")),
                     sequence=5, tick=t0 + 40, boot=boot, owner=owner_epoch),
    ]
    resumed = stamp_record(
        M6ProducerEvent(run_id=spec.run_id, spec_sha256=spec_sha, producer_kind="owner",
                        producer_epoch_sha256=owner_epoch_2, producer_sequence=1,
                        payload=M6OwnerResumed(clock=spec.clock, t0_ticks=t0, deadline_ticks=spec.deadline_ticks,
                                               previous_owner_epoch_sha256=owner_epoch)),
        sequence=6, tick=t0 + 50, boot=boot, owner=owner_epoch_2)
    journal_lines = [r.canonical_bytes().decode() for r in records]

    stamp_mismatch = json.loads(records[1].canonical_bytes())
    stamp_mismatch["stamp"]["producer_event_sha256"] = label_hash("wrong-producer-hash")
    seq_gap = json.loads(records[1].canonical_bytes())
    seq_gap["stamp"]["sequence"] = 3
    tick_regress = json.loads(records[1].canonical_bytes())
    tick_regress["stamp"]["received_qpc_ticks"] = t0 - 1
    role_mismatch = json.loads(records[1].canonical_bytes())
    role_mismatch["event"]["producer_kind"] = "service_runner"
    role_mismatch["event"]["producer_epoch_sha256"] = runner_epoch
    role_mismatch["stamp"]["producer_event_sha256"] = M6ProducerEvent.model_validate(role_mismatch["event"]).canonical_sha256()
    not_first = json.loads(records[1].canonical_bytes())
    not_first["stamp"]["sequence"] = 1
    # "layer" says which native reader must refuse the record: the append-only
    # journal (physical order/binding damage) or the run control replay (role
    # and business semantics). Codes are the public refusal identifiers.
    journal_negatives = [
        {"name": "stamp_hash_not_binding_producer", "layer": "journal", "after_prefix": 1,
         "record": canonical(stamp_mismatch), "expect": "malformed_event_record"},
        {"name": "sequence_gap", "layer": "journal", "after_prefix": 1, "record": canonical(seq_gap),
         "expect": "physical_owner_order_invalid"},
        {"name": "tick_regression", "layer": "journal", "after_prefix": 1, "record": canonical(tick_regress),
         "expect": "physical_owner_order_invalid"},
        {"name": "owner_event_from_runner_role", "layer": "control", "after_prefix": 1, "record": canonical(role_mismatch),
         "expect": "journal_role_mismatch"},
        {"name": "run_started_not_first", "layer": "journal", "after_prefix": 0, "record": canonical(not_first),
         "expect": "run_start_missing"},
        {"name": "noncanonical_record_spacing", "layer": "journal", "after_prefix": 1,
         "record": json.dumps(json.loads(journal_lines[1]), sort_keys=True), "expect": "malformed_event_record"},
    ]

    bind_request = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-bind-1",
                                  command=M6BindOwner(anchor_sha256=anchor.canonical_sha256()))
    append_request = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-append-1",
                                    command=M6AppendObservation(event=records[2].event))
    drain_event = M6ProducerEvent(run_id=spec.run_id, spec_sha256=spec_sha, producer_kind="quality_verifier",
                                  producer_epoch_sha256=verifier_epoch, producer_sequence=1,
                                  payload=M6VerifierDrained(drain_receipt_sha256=label_hash("drain-receipt")))
    drain_request_a = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-drain-a",
                                     command=M6AppendObservation(event=drain_event))
    drain_request_b = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-drain-b",
                                     command=M6AppendObservation(event=drain_event))
    ack_request = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-ack-1", command=M6AdmissionClosedAck(
        runner_epoch_sha256=runner_epoch, last_producer_sequence=2, admitted_attempt_count=1, unresolved_claim_count=0,
        reconciliation_receipt_sha256=label_hash("reconciliation-receipt")))
    close_request = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-close-1", command=M6CloseOwner(
        ownership_receipt_sha256=label_hash("ownership-receipt"), residual_count=0, children_exited=True,
        reason="stop_requested"))
    status_request = M6OwnerRequest(run_id=spec.run_id, spec_sha256=spec_sha, request_id="req-status-1",
                                    command=M6OwnerControl(kind="status"))
    cross_run_append = json.loads(append_request.canonical_bytes())
    cross_run_append["run_id"] = "run-other"

    status = M6OwnerStatus(run_id=spec.run_id, spec_sha256=spec_sha, anchor_sha256=anchor.canonical_sha256(),
                           owner_process_epoch_sha256=owner_epoch, observed_qpc_ticks=t0 + 20, state="open",
                           last_sequence=3, admission_valid_until_ticks=t0 + 20 + QPC_FREQUENCY)
    reply = M6OwnerReply(request_sha256=append_request.canonical_sha256(), outcome="ok", status=status,
                         record=records[2], error_code=None)

    drain_command = json.loads(drain_request_a.canonical_bytes())["command"]
    drain_identity = canonical({"command": drain_command, "run_id": spec.run_id, "spec_sha256": spec_sha})
    drain_a_sha = drain_request_a.canonical_sha256()
    drain_b_sha = drain_request_b.canonical_sha256()

    attempt_ids = ["attempt-b", "attempt-a", "attempt-é-1"]
    attempt_lines = sorted(sha_text(item) for item in attempt_ids)
    attempt_set_sha = sha_bytes("".join(line + "\n" for line in attempt_lines).encode("utf-8"))

    return {
        "contract_version": FIXTURE_VERSION,
        "generator": "tests/fixtures/m6_owner/generate_m6_owner_fixtures.py",
        "qpc_frequency_hz": QPC_FREQUENCY,
        "identities": {
            "owner_epoch": owner_epoch, "owner_epoch_2": owner_epoch_2,
            "runner_epoch": runner_epoch, "verifier_epoch": verifier_epoch,
            "boot_identity_sha256": boot, "spec_sha256": spec_sha,
            "anchor_sha256": anchor.canonical_sha256(),
            "e2e_spec_sha256": e2e_spec.canonical_sha256(), "e2e_anchor_sha256": e2e_anchor.canonical_sha256(),
        },
        "clock": json.loads(spec.clock.canonical_bytes()),
        "service": {"spec": spec.canonical_bytes().decode(), "anchor": anchor_text,
                    "carry_in_scalar_order": list(unicode_ids)},
        "e2e": {"spec": e2e_spec.canonical_bytes().decode(), "anchor": e2e_anchor.canonical_bytes().decode()},
        "binding_negatives": binding_negatives,
        "journal": {
            "records": journal_lines,
            "record_hashes": [sha_text(line) for line in journal_lines],
            "resumed_record": resumed.canonical_bytes().decode(),
            "producer_events": [r.event.canonical_bytes().decode() for r in records],
            "producer_hashes": [r.event.canonical_sha256() for r in records],
            "negatives": journal_negatives,
        },
        "requests": {
            "bind": bind_request.canonical_bytes().decode(),
            "append": append_request.canonical_bytes().decode(),
            "append_sha256": append_request.canonical_sha256(),
            "status": status_request.canonical_bytes().decode(),
            "drain_a": drain_request_a.canonical_bytes().decode(),
            "drain_b": drain_request_b.canonical_bytes().decode(),
            "admission_closed": ack_request.canonical_bytes().decode(),
            "close": close_request.canonical_bytes().decode(),
            "cross_run_append_rejected": canonical(cross_run_append),
        },
        "reply": {"canonical": reply.canonical_bytes().decode(), "sha256": reply.canonical_sha256()},
        "pending_drain": {
            "identity_raw": drain_identity, "identity_sha256": sha_text(drain_identity),
            "request_a_sha256": drain_a_sha, "request_b_sha256": drain_b_sha,
            "chain_after_two": sha_text(drain_a_sha + "\n" + drain_b_sha),
        },
        "attempt_set": {"ids": attempt_ids, "sha256": attempt_set_sha},
        "quote_cases": [
            {"value": value, "quoted": json.dumps(value, ensure_ascii=False)}
            for value in ("plain", "tab\tnl\nquote\"back\\", CONTROL_SAMPLE, "uni-é-\U0001f600")
        ],
    }


def render() -> str:
    return json.dumps(generate(), ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="verify an existing output is byte-identical")
    args = parser.parse_args()
    text = render()
    target = args.output or Path(__file__).with_name("wire-vectors.v2.json")
    if args.check:
        existing = target.read_text(encoding="utf-8")
        if existing != text:
            print("fixture drift: " + str(target))
            return 1
        print("fixture current: " + str(target))
        return 0
    target.write_text(text, encoding="utf-8")
    print("wrote " + str(target) + " sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
