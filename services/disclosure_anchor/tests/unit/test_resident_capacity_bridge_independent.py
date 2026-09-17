"""Independent acceptance for the host lane's capacity bridge.

Wire v2 stopped mirroring the serving API's capacity rules on Windows. The
exporter forwards the producer's exact health and HTTP bytes plus its own vLLM
digest, and `project_queue_vllm` evaluates them once, here, through the shared
canonical validator against the release's frozen `MineruCapacityConfig` and the
API process the Linux sampler measured in the same frame.

These cases are about that boundary: which external authority a forwarded reply
is judged against, and what the projection is allowed to turn into frame values.
They are semantic, not snapshots - every document is composed from the shared
capacity fixtures for the capacity under test, so a legal capacity this file has
never seen is accepted and an illegal one is refused for a named reason. No
runtime file, machine identity or live payload is read.
"""

from copy import deepcopy
import json
import unittest

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.windows_resident_telemetry import (
    HostQueueBinding, QueueVllmWireObservation, decode_windows_resident_sample, project_queue_vllm,
)
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES, canonical_payload, capacity_payload
from tests._mineru_capacity_health_fixture import capacity_health_payload


BASELINE = decode_mineru_capacity_config(CAPACITY_BYTES)
# The shape the current deployment runs, expressed as a capacity rather than copied
# from it: seven parse owners, eight nonterminal, one finalizer, fourteen HTTP.
DEPLOYED_CHANGES = {"parse_active_limit": 7, "total_nonterminal_limit": 8,
                    "finalizer_active_limit": 1, "final_http_limit_per_loop": 14}
HTTP_CONTRACT = "mineru.api-http-request-snapshot.v1"
VLLM = {"vllm_requests_running": 2, "vllm_requests_waiting": 1,
        "vllm_kv_cache_usage_ratio": 0.25, "vllm_preemptions_total": 3}


def capacity(**changes):
    """One legal capacity config; `changes` are the external authority under test."""
    return decode_mineru_capacity_config(canonical_payload(capacity_payload(**changes)))


def health_for(config, *, busy=False):
    """A serving reply that a correct API with this capacity would produce.

    Idle by default. `busy` fills the whole nonterminal envelope with N parse
    owners and F finalizers, which is the interesting case: processing exceeds
    the parse limit legally, and admission is closed because capacity is full.
    """
    document = capacity_health_payload()
    parse, total = config.parse_active_limit, config.total_nonterminal_limit
    finalizer, http_limit = config.finalizer_active_limit, config.final_http_limit_per_loop
    document.update(
        max_concurrent_requests=parse, max_pending_tasks_requested=total,
        max_pending_tasks_effective=total, processing_window_size=config.processing_window_size,
        queued_tasks=0, processing_tasks=parse + finalizer if busy else 0,
    )
    document["task_protocol_runtime"].update(
        capacity_config_sha256=config.sha256,
        task_result_reservation_bytes=config.result_reservation_bytes,
        max_unacked_result_bytes=config.max_unacked_result_bytes,
    )
    owned = parse + finalizer if busy else 0
    document["task_admission"].update(
        nonterminal_limit=total, ingress_tasks=0, accepted_pending_tasks=0,
        accepted_processing_tasks=parse if busy else 0,
        accepted_finalizing_tasks=finalizer if busy else 0,
        durable_nonterminal_tasks=owned, routeless_accepted_tasks=0, ingress_cleanup_tasks=0,
        unowned_ingress_tasks=0, scheduled_tasks=owned, queue_depth=0, active_processors=owned,
        recovery_overcommitted=False, admission_open=not (busy and owned == total),
        blocked_reason="capacity_full" if busy and owned == total else None,
    )
    observation = document["capacity_observation"]
    observation["capacity_config_sha256"] = config.sha256
    observation["resolved_limits"] = {
        "parse_active_limit": parse, "total_nonterminal_limit": total,
        "finalizer_active_limit": finalizer, "final_http_limit_per_loop": http_limit,
        "result_reservation_bytes": config.result_reservation_bytes,
        "max_unacked_result_bytes": config.max_unacked_result_bytes,
    }
    observation["stage_counters"] = {
        "result_capacity_waiting": 0, "parse_waiting": 0, "parse_active": parse if busy else 0,
        "finalizer_waiting": 0, "finalizer_active": finalizer if busy else 0,
    }
    observation["http_counters"] = {"active_requests": 0, "pending_requests": 0}
    return document


def binding_for(config, document, **changes):
    owner = document["capacity_observation"]["owner"]
    fields = {
        "expected_capacity": config, "serving_namespace_pid": owner["process_id"],
        "api_boot_id": owner["boot_id"], "api_start_ticks": owner["process_start_ticks"],
        "task_retention_seconds": document["task_retention_seconds"],
        "task_cleanup_interval_seconds": document["task_cleanup_interval_seconds"],
    }
    fields.update(changes)
    return HostQueueBinding(**fields)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def wire_for(document, *, process_id, active=1, pending=2, health_text=None, http_text=None,
             vllm=None, status="supported", reason=None):
    values = None
    if status == "supported":
        values = {
            "api_health": canonical(document) if health_text is None else health_text,
            "api_http": canonical({
                "contract_version": HTTP_CONTRACT, "process_id": process_id,
                "active_requests": active, "pending_requests": pending,
            }) if http_text is None else http_text,
            "vllm": dict(VLLM if vllm is None else vllm),
        }
    return QueueVllmWireObservation.model_validate(
        {"status": status, "reason": reason, "values": values})


class ResidentCapacityBridgeTests(unittest.TestCase):
    def project(self, config, document, **binding_changes):
        return project_queue_vllm(
            wire_for(document, process_id=document["capacity_observation"]["owner"]["process_id"]),
            binding=binding_for(config, document, **binding_changes))

    def test_an_idle_and_a_full_envelope_both_project_their_own_counts(self):
        for changes in ({}, DEPLOYED_CHANGES):
            config = capacity(**changes)
            with self.subTest(parse=config.parse_active_limit, total=config.total_nonterminal_limit):
                idle = self.project(config, health_for(config))
                self.assertEqual(idle.status, "supported")
                assert idle.values is not None
                self.assertEqual(idle.values.api_nonterminal_tasks, 0)
                self.assertEqual(idle.values.api_max_pending_tasks, config.total_nonterminal_limit)
                # Full: N parse owners plus F finalizers, so processing legally exceeds N and
                # admission is closed on capacity. A frame is still owed for that instant.
                document = health_for(config, busy=True)
                full = self.project(config, document)
                assert full.values is not None
                self.assertEqual(full.values.api_processing_tasks,
                                 config.parse_active_limit + config.finalizer_active_limit)
                self.assertGreater(full.values.api_processing_tasks, config.parse_active_limit)
                self.assertEqual(full.values.api_nonterminal_tasks, config.total_nonterminal_limit)
                self.assertEqual(document["task_admission"]["blocked_reason"], "capacity_full")
                # The HTTP counts are the snapshot's, not the health's.
                self.assertEqual((full.values.api_http_active_requests,
                                  full.values.api_http_pending_requests), (1, 2))
                self.assertEqual(full.values.vllm_kv_cache_usage_ratio, VLLM["vllm_kv_cache_usage_ratio"])

    def test_a_legal_capacity_is_judged_only_against_its_own_external_authority(self):
        # Each alternative is a different external authority: accepted when the frozen
        # capacity is the one the reply was produced under, refused when it is not - even
        # though the reply is internally self-consistent and carries matching hashes.
        alternatives = (
            {"final_http_limit_per_loop": 28},
            {"finalizer_active_limit": 2},
            {"result_reservation_bytes": 134217728, "max_unacked_result_bytes": 4294967296},
            {"max_unacked_result_bytes": 4294967296},
            {"parse_active_limit": 1},
            {"processing_window_size": 32},
            {"total_nonterminal_limit": 8, "final_http_limit_per_loop": 14},
            {"omp_num_threads": 8},
        )
        for changes in alternatives:
            with self.subTest(changes=tuple(sorted(changes))):
                config = capacity(**changes)
                self.assertNotEqual(config.sha256, BASELINE.sha256)
                document = health_for(config)
                projected = self.project(config, document)
                self.assertEqual(projected.status, "supported")
                with self.assertRaises(ValueError):
                    project_queue_vllm(
                        wire_for(document, process_id=document["capacity_observation"]["owner"]["process_id"]),
                        binding=binding_for(BASELINE, document, expected_capacity=BASELINE))

    def test_a_reply_that_agrees_with_itself_but_not_with_the_release_is_refused(self):
        document = health_for(BASELINE)
        forged = "sha256:" + "0" * 64
        document["task_protocol_runtime"]["capacity_config_sha256"] = forged
        document["capacity_observation"]["capacity_config_sha256"] = forged
        with self.assertRaises(ValueError):
            self.project(BASELINE, document)
        # A resolved limit that drifts without the identity changing is equally refused.
        drifted = health_for(BASELINE)
        drifted["capacity_observation"]["resolved_limits"]["final_http_limit_per_loop"] += 1
        with self.assertRaises(ValueError):
            self.project(BASELINE, drifted)

    def test_the_reply_must_come_from_the_api_process_the_sampler_measured(self):
        document = health_for(BASELINE)
        owner = document["capacity_observation"]["owner"]
        for change in ({"serving_namespace_pid": owner["process_id"] + 1},
                       {"api_boot_id": "99999999-8888-4777-8666-555555555555"},
                       {"api_start_ticks": owner["process_start_ticks"] + 1}):
            with self.subTest(change=tuple(change)), self.assertRaises(ValueError):
                self.project(BASELINE, document, **change)
        # The HTTP snapshot has to name the same process as well.
        with self.assertRaises(ValueError):
            project_queue_vllm(wire_for(document, process_id=owner["process_id"] + 1),
                               binding=binding_for(BASELINE, document))

    def test_a_draining_owner_or_closed_admission_is_drift_rather_than_a_sample(self):
        draining = health_for(BASELINE)
        draining["capacity_observation"]["owner_control"].update(
            foreign_loop_observed=True, soft_drain_requested=True, soft_drain_applied=True,
            trigger="foreign_event_loop")
        draining["task_admission"].update(admission_open=False, blocked_reason="shutting_down")
        with self.assertRaises(ValueError):
            self.project(BASELINE, draining)
        # Capacity-full is the one closed admission that is still a legal instant.
        full = health_for(capacity(**DEPLOYED_CHANGES), busy=True)
        self.assertEqual(self.project(capacity(**DEPLOYED_CHANGES), full).status, "supported")

    def test_retention_and_cleanup_come_from_the_profile_not_from_the_reply(self):
        document = health_for(BASELINE)
        for change in ({"task_retention_seconds": document["task_retention_seconds"] + 1},
                       {"task_cleanup_interval_seconds": document["task_cleanup_interval_seconds"] + 1}):
            with self.subTest(change=tuple(change)), self.assertRaises(ValueError):
                self.project(BASELINE, document, **change)

    def test_an_unsupported_observation_is_preserved_and_never_invented(self):
        unsupported = wire_for(None, process_id=0, status="unsupported", reason="collector_unsupported")
        projected = project_queue_vllm(unsupported, binding=binding_for(BASELINE, health_for(BASELINE)))
        self.assertEqual((projected.status, projected.reason, projected.values),
                         ("unsupported", "collector_unsupported", None))

    def test_malformed_old_or_oversized_replies_never_become_values(self):
        document = health_for(BASELINE)
        text = canonical(document)
        stale = deepcopy(document)
        stale["task_protocol_runtime"]["schema"] = "mineru-task-runtime.v2"
        cases = {
            "duplicate_key": text.replace('"status":"healthy"', '"status":"healthy","status":"healthy"', 1),
            "nonfinite_number": text.replace('"queued_tasks":0', '"queued_tasks":NaN', 1),
            "malformed_number": text.replace('"queued_tasks":0', '"queued_tasks":01', 1),
            "truncated": text[:-1],
            "old_runtime_schema": canonical(stale),
            "oversized": canonical(document).replace(
                "clock_gettime(CLOCK_MONOTONIC)", "clock_gettime(CLOCK_MONOTONIC)" + "x" * 9000, 1),
        }
        for name, health_text in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                project_queue_vllm(
                    wire_for(document, process_id=document["capacity_observation"]["owner"]["process_id"],
                             health_text=health_text),
                    binding=binding_for(BASELINE, document))
        # The forwarded HTTP snapshot is held to the same standard.
        for http_text in ('{"contract_version":"mineru.api-http-request-snapshot.v2","process_id":123,'
                          '"active_requests":1,"pending_requests":2}',
                          '{"contract_version":"mineru.api-http-request-snapshot.v1","process_id":123,'
                          '"active_requests":1.0,"pending_requests":2}',
                          '{"contract_version":"mineru.api-http-request-snapshot.v1","process_id":123,'
                          '"active_requests":1,"pending_requests":2,"extra":0}',
                          '{"contract_version":"mineru.api-http-request-snapshot.v1","process_id":123,'
                          '"active_requests":1}'):
            with self.subTest(http=http_text[60:96]), self.assertRaises(ValueError):
                project_queue_vllm(
                    wire_for(document, process_id=123, http_text=http_text),
                    binding=binding_for(BASELINE, document))

    def test_the_wire_sample_itself_is_decoded_from_exact_canonical_bytes(self):
        document = health_for(BASELINE)
        sample = {
            "contract_version": "mineru.windows-resident-telemetry.v2", "lane": "host_slow",
            "sequence": 1, "observed_at_utc": "2026-09-17T00:00:00+00:00", "sampled_monotonic_ns": 1,
            "identity": {name: "sha256:" + str(index) * 64 for index, name in enumerate((
                "exporter_source_sha256", "host_assignment_identity_sha256", "boot_identity_sha256",
                "runtime_bundle_identity_sha256", "process_profile_sha256",
                "clock_domain_identity_sha256", "exporter_process_epoch_sha256"), start=1)},
            "api_process": {"status": "unsupported", "reason": "collector_unsupported", "values": None},
            "host_cgroup": {"status": "unsupported", "reason": "collector_unsupported", "values": None},
            "queue_vllm": json.loads(wire_for(
                document, process_id=123).model_dump_json()),
        }
        payload = canonical(sample).encode("utf-8")
        decoded = decode_windows_resident_sample(payload, lane="host_slow")
        self.assertEqual(decoded.lane, "host_slow")
        assert decoded.queue_vllm.values is not None
        # The producer's bytes survive the transport unchanged, so the validator above sees
        # exactly what the serving API answered.
        self.assertEqual(decoded.queue_vllm.values.api_health, canonical(document))
        projected = project_queue_vllm(decoded.queue_vllm, binding=binding_for(BASELINE, document))
        self.assertEqual(projected.status, "supported")
        broken = {
            "duplicate_field": payload.replace(b'"sequence":1', b'"sequence":1,"sequence":1', 1),
            "noncanonical_spacing": payload.replace(b'"lane":', b'"lane" :', 1),
            "sequence_below_one": payload.replace(b'"sequence":1', b'"sequence":0', 1),
            "observed_at_not_utc": payload.replace(b'+00:00', b'+01:00', 1),
            "superseded_wire_version": payload.replace(b'telemetry.v2', b'telemetry.v1', 1),
            "wrong_lane_model": payload,
        }
        for name, raw in broken.items():
            lane = "gpu_fast" if name == "wrong_lane_model" else "host_slow"
            with self.subTest(case=name), self.assertRaises(ValueError):
                decode_windows_resident_sample(raw, lane=lane)


if __name__ == "__main__":
    unittest.main()
