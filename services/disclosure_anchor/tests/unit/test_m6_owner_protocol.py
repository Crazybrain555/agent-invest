from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.m6_owner_protocol import (
    M6LeasePolicy, M6LineOwnerTransport, M6OwnerClient, M6OwnerProtocolError, M6OwnerRejected,
)
from disclosure_anchor.adapters.runtime.m6_owner_ssh import m6_ssh_owner_transport
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.application.contracts.m6_owner import (
    M6AdmissionClosedAck, M6CloseOwner, M6OwnerAnchor, M6OwnerControl, M6OwnerReply, M6OwnerRequest, M6OwnerStatus,
)
from tests.m6_support import RunExample, changed, sha


class Clock:
    def __init__(self):
        self.ns = 1_000_000_000

    def __call__(self):
        return self.ns


class ReplyFixture:
    """Wire response fixture only; no simulated durability or platform proof."""

    def __init__(self, example, clock):
        self.example, self.clock = example, clock
        spec = example.spec
        self.anchor = M6OwnerAnchor(
            run_id=spec.run_id, clock=spec.clock, owner_process_epoch_sha256=example.owner_epoch,
            owner_source_sha256=spec.runtime.owner_source_sha256,
            gpu_device_identity_sha256=spec.runtime.gpu_device_identity_sha256,
            **{k: getattr(spec, k) for k in ("t0_ticks", "planned_seconds", "deadline_ticks", "max_close_ticks", "resources")},
        )
        self.status = M6OwnerStatus(run_id=spec.run_id, spec_sha256=spec.canonical_sha256(),
            anchor_sha256=self.anchor.canonical_sha256(), owner_process_epoch_sha256=example.owner_epoch,
            observed_qpc_ticks=1000, state="open", last_sequence=100, admission_valid_until_ticks=1010)
        self.elapsed_ns = 0
        self.record = None
        self.outcome = "ok"
        self.error = None
        self.mutate = lambda raw: raw
        self.closed = False

    def exchange(self, raw):
        request = M6OwnerRequest.from_canonical_bytes(raw, maximum_bytes=65536)
        self.clock.ns += self.elapsed_ns
        result = M6OwnerReply(request_sha256=request.canonical_sha256(), outcome=self.outcome,
            status=self.status, record=self.record, error_code=self.error)
        return self.mutate(result.canonical_bytes())

    def close(self):
        self.closed = True


class M6OwnerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.example, self.clock = RunExample(service=True), Clock()
        self.example.spec = changed(self.example.spec, resources=changed(self.example.spec.resources, stop_admission_budget_ticks=20))
        self.wire = ReplyFixture(self.example, self.clock)
        self.client = M6OwnerClient(anchor=self.wire.anchor, spec=self.example.spec, transport=self.wire,
            caller_role="service_runner", producer_epoch_sha256=sha("service_runner"), continuous_ns=self.clock,
            lease_policy=M6LeasePolicy(stop_propagation_reserve_ns=500_000_000))

    def test_bootstrap_interval_and_runtime_bind_before_control(self):
        self.wire.anchor.assert_spec(self.example.spec)
        for update in (
            {"clock": changed(self.example.clock, host_assignment_identity_sha256=sha("other-host"))},
            {"max_close_ticks": self.example.spec.max_close_ticks+1},
            {"runtime": changed(self.example.spec.runtime, owner_source_sha256=sha("other-code"))},
            {"runtime": changed(self.example.spec.runtime, gpu_device_identity_sha256=sha("other-device"))},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.wire.anchor.assert_spec(changed(self.example.spec, **update))
        with self.assertRaises(ValueError):
            changed(self.wire.anchor, deadline_ticks=100)

    def test_lease_starts_at_request_send_and_accounts_for_rtt_margin_and_drift(self):
        self.wire.elapsed_ns = 300_000_000
        self.assertTrue(self.client.refresh_admission())
        self.clock.ns = 1_989_000_999  # floor(1s/1.001) - 10ms, anchored at send.
        self.assertFalse(self.client.admission_allowed())

    def test_reply_slower_than_grant_cannot_create_a_fresh_lease(self):
        self.wire.elapsed_ns = 1_000_000_000
        self.assertFalse(self.client.refresh_admission())

    def test_sleep_or_expiry_cannot_preserve_admission(self):
        self.assertTrue(self.client.refresh_admission())
        self.clock.ns += 60_000_000_000  # injected continuous clock includes sleep.
        self.assertFalse(self.client.admission_allowed())

    def test_clock_regression_and_transport_loss_latch_admission_closed_but_allow_drain(self):
        for cause in ("clock", "transport"):
            with self.subTest(cause=cause):
                self.setUp()
                self.assertTrue(self.client.refresh_admission())
                if cause == "clock":
                    self.clock.ns -= 1
                    with self.assertRaises(M6OwnerProtocolError):
                        self.client.admission_allowed()
                    self.clock.ns += 10
                else:
                    def fail(raw):
                        raise EOFError("connection lost after possible durable append")
                    self.wire.mutate = fail
                    with self.assertRaises(EOFError):
                        self.client.refresh_admission()
                    self.wire.mutate = lambda raw: raw
                self.assertFalse(self.client.refresh_admission())
                self.wire.status = changed(self.wire.status, state="draining", admission_valid_until_ticks=None)
                self.assertEqual(self.client.request(M6OwnerControl(kind="status")).status.state, "draining")

    def test_identity_nonce_canonical_shape_and_bounds_are_checked(self):
        def mutate_json(raw, name, value):
            data = json.loads(raw)
            data[name] = value
            return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        for mutation in (
            lambda raw: mutate_json(raw, "request_sha256", sha("stale-response")),
            lambda raw: mutate_json(raw, "unrecognized", True),
            lambda raw: b" " + raw,
            lambda raw: raw[:-1] + b',"outcome":"ok"}',
            lambda raw: b"x" * 65537,
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.wire.mutate = mutation
                with self.assertRaises(ValueError):
                    self.client.refresh_admission()
                self.assertFalse(self.client.admission_allowed())
        for updates in (
            {"anchor_sha256": sha("another-boot-or-anchor")},
            {"owner_process_epoch_sha256": sha("unconfirmed-resume")},
            {"spec_sha256": sha("another-spec")},
            {"admission_valid_until_ticks": self.example.spec.deadline_ticks+1},
        ):
            with self.subTest(updates=updates):
                self.setUp()
                self.wire.status = changed(self.wire.status, **updates)
                with self.assertRaises(M6OwnerProtocolError):
                    self.client.refresh_admission()

    def test_stop_is_irreversible_and_cannot_reopen_from_a_later_response(self):
        self.assertTrue(self.client.refresh_admission())
        self.wire.status = changed(self.wire.status, state="stopping", admission_valid_until_ticks=None)
        self.assertFalse(self.client.refresh_admission())
        self.wire.status = changed(self.wire.status, state="open", admission_valid_until_ticks=1010)
        with self.assertRaises(M6OwnerProtocolError):
            self.client.refresh_admission()

    def test_counter_and_owner_sequence_regressions_fail_closed(self):
        for updates in ({"observed_qpc_ticks": 999}, {"last_sequence": 99}):
            with self.subTest(updates=updates):
                self.setUp()
                self.client.refresh_admission()
                self.wire.status = changed(self.wire.status, **updates)
                with self.assertRaises(M6OwnerProtocolError):
                    self.client.refresh_admission()

    def test_exact_observation_retry_returns_old_stamp_and_conflict_retains_evidence(self):
        self.example.start()
        self.example.document()
        record = next(r for r in self.example.records if r.event.payload.kind == "attempt_admitted")
        self.wire.record = record
        self.assertEqual(self.client.append(record.event), record)
        self.assertEqual(self.client.append(record.event), record)
        self.wire.outcome, self.wire.error = "conflict", "producer_event_conflict"
        with self.assertRaises(M6OwnerRejected) as result:
            self.client.append(record.event)
        self.assertEqual(result.exception.reply.record, record)
        self.assertFalse(self.client.admission_allowed())

    def test_wrong_stamp_boot_bytes_or_producer_never_acknowledged(self):
        self.example.start()
        self.example.document()
        record = next(r for r in self.example.records if r.event.payload.kind == "attempt_admitted")
        self.wire.record = changed(record, stamp=changed(record.stamp, boot_identity_sha256=sha("other-boot")))
        with self.assertRaises(M6OwnerProtocolError):
            self.client.append(record.event)
        self.wire.record = next(r for r in self.example.records if r.event.payload.kind == "remote_accepted")
        with self.assertRaises(M6OwnerProtocolError):
            self.client.append(record.event)
        with self.assertRaises(M6OwnerProtocolError):
            self.client.append(self.example.records[0].event)

    def test_close_revokes_lease_and_only_claims_local_transport_exit(self):
        self.client.refresh_admission()
        self.client.close()
        self.client.close()
        self.assertTrue(self.wire.closed)
        with self.assertRaises(RuntimeError):
            self.client.admission_allowed()

    def test_lease_configuration_is_finite_and_conservative(self):
        for updates in ({"maximum_lease_ns": True}, {"uncertainty_margin_ns": 0},
                        {"maximum_clock_drift_ppm": 0}, {"maximum_lease_ns": 30_000_000_001}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                M6LeasePolicy(stop_propagation_reserve_ns=500_000_000, **updates)

    def test_lease_must_leave_room_for_actual_claim_return_and_stop_ack(self):
        # Reproduce the review finding: a 1s grant used to be accepted against
        # a 1s total stop budget, leaving no room for claim completion/ACK.
        spec = changed(self.example.spec, resources=changed(self.example.spec.resources, stop_admission_budget_ticks=10))
        anchor = changed(self.wire.anchor, resources=spec.resources)
        self.wire.anchor = anchor
        self.wire.status = changed(self.wire.status, anchor_sha256=anchor.canonical_sha256(), spec_sha256=spec.canonical_sha256())
        client = M6OwnerClient(anchor=anchor, spec=spec, transport=self.wire, caller_role="service_runner",
            producer_epoch_sha256=sha("service_runner"), continuous_ns=self.clock,
            lease_policy=M6LeasePolicy(stop_propagation_reserve_ns=500_000_000))
        with self.assertRaisesRegex(M6OwnerProtocolError, "stop propagation budget"):
            client.refresh_admission()
        for reserve in (990_000_000, 1_000_000_000):
            with self.subTest(reserve=reserve), self.assertRaises(ValueError):
                M6OwnerClient(anchor=anchor, spec=spec, transport=self.wire, caller_role="service_runner",
                    producer_epoch_sha256=sha("service_runner"), continuous_ns=self.clock,
                    lease_policy=M6LeasePolicy(stop_propagation_reserve_ns=reserve))

    def test_nonlease_observation_roundtrip_preserves_only_original_unexpired_grant(self):
        self.assertTrue(self.client.refresh_admission())
        self.wire.elapsed_ns = 100_000_000
        self.client.request(M6OwnerControl(kind="status"))
        self.assertTrue(self.client.admission_allowed())
        self.clock.ns = 1_989_000_999
        self.assertFalse(self.client.admission_allowed())

    def test_control_roles_bind_and_ack_incarnation_are_explicit(self):
        with self.assertRaises(M6OwnerProtocolError):
            self.client.bind()
        controller = M6OwnerClient(anchor=self.wire.anchor, spec=self.example.spec, transport=self.wire,
            caller_role="controller", producer_epoch_sha256=sha("controller"), continuous_ns=self.clock,
            lease_policy=M6LeasePolicy(stop_propagation_reserve_ns=500_000_000))
        self.assertEqual(controller.bind().outcome, "ok")
        ack = M6AdmissionClosedAck(runner_epoch_sha256=sha("service_runner"), last_producer_sequence=0,
            admitted_attempt_count=0, unresolved_claim_count=0, reconciliation_receipt_sha256=sha("reconciled"))
        with self.assertRaises(M6OwnerProtocolError):
            controller.request(ack)
        with self.assertRaises(M6OwnerProtocolError):
            self.client.request(changed(ack, runner_epoch_sha256=sha("other-runner")))
        self.wire.status = changed(self.wire.status, state="draining", admission_valid_until_ticks=None)
        self.assertEqual(self.client.request(ack).outcome, "ok")
        self.wire.status = changed(self.wire.status, state="closed")
        close = M6CloseOwner(ownership_receipt_sha256=sha("closure"), residual_count=0,
                            children_exited=True, reason="stop_requested")
        self.assertEqual(controller.request(close).status.state, "closed")

    def test_rejected_cannot_conceal_a_stamped_record_and_pre_t0_stamp_is_rejected(self):
        self.example.start()
        self.example.document()
        record = next(r for r in self.example.records if r.event.payload.kind == "attempt_admitted")
        with self.assertRaises(ValueError):
            M6OwnerReply(request_sha256=sha("request"), outcome="rejected", status=self.wire.status,
                record=record, error_code="not_accepted")
        self.wire.record = changed(record, stamp=changed(record.stamp, received_qpc_ticks=99))
        with self.assertRaises(M6OwnerProtocolError):
            self.client.append(record.event)


class M6OwnerLineTransportTests(unittest.TestCase):
    def test_pinned_ssh_is_lazy_and_its_actual_session_is_closed(self):
        sent, connections, closed = [], [], []
        class Channel:
            def settimeout(self, value):
                pass
            def sendall(self, value):
                sent.append(value)
            def recv(self, maximum):
                return b'{}\n'
            def close(self):
                closed.append("channel")
        class Session:
            def __init__(self, config, *, remote_port, timeout):
                connections.append((config, remote_port, timeout))
            def open_channel(self, timeout):
                return Channel()
            def close(self):
                closed.append("session")
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory)/"caller-token"
            token.write_text("a"*64+"\n")
            token.chmod(0o600)
            config = ResidentSSHConfig(address="127.0.0.1", port=22, username="operator",
                private_key_path=str(Path(directory)/"key"), known_hosts_path=str(Path(directory)/"known-hosts"))
            with patch("disclosure_anchor.adapters.runtime.m6_owner_ssh._Session", Session):
                transport = m6_ssh_owner_transport(config=config, token_path=str(token), remote_port=39836, continuous_ns=Clock())
                self.assertEqual(connections, [])
                self.assertEqual(transport.exchange(b'{}'), b'{}')
                transport.close()
            self.assertEqual(connections[0][:2], (config, 39836))
            self.assertEqual(closed, ["channel", "session"])
            self.assertEqual(sent, [b"M6-AUTH/1 "+b"a"*64+b"\n{}\n"])
            token.chmod(0o644)
            with self.assertRaises(ValueError):
                m6_ssh_owner_transport(config=config, token_path=str(token), remote_port=39836, continuous_ns=Clock())

    def test_failed_close_is_retained_for_cleanup_and_channel_is_never_reused(self):
        for cause in ("send", "direct_close"):
            with self.subTest(cause=cause):
                class BrokenChannel:
                    sends = 0
                    def settimeout(self, timeout):
                        pass
                    def sendall(self, value):
                        self.sends += 1
                        if cause == "send":
                            raise OSError("broken send")
                    def recv(self, maximum):
                        return b'{}\n'
                    def close(self):
                        raise OSError("close also failed")
                channel = BrokenChannel()
                transport = M6LineOwnerTransport(token="a"*64, open_channel=lambda timeout: channel,
                    close_session=lambda: None, continuous_ns=Clock())
                if cause == "direct_close":
                    self.assertEqual(transport.exchange(b'{}'), b'{}')
                    with self.assertRaises(BaseExceptionGroup):
                        transport.close()
                for _ in range(2):
                    with self.assertRaises(BaseExceptionGroup):
                        transport.exchange(b'{}')
                self.assertEqual(channel.sends, 1)
                with self.assertRaises(BaseExceptionGroup):
                    transport.close()

    def test_actual_stream_preserves_lf_requests_and_reuses_channel_without_secret_in_reply(self):
        local, remote = socket.socketpair()
        received, errors, open_calls, session_closed = [], [], [], []

        def server():
            try:
                with remote, remote.makefile("rb") as stream:
                    for _ in range(2):
                        received.append(stream.readline())
                        received.append(stream.readline())
                        remote.sendall(b'{"ok":')
                        remote.sendall(b'true}\n')
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=server)
        thread.start()
        def connect(timeout):
            open_calls.append(timeout)
            return local
        transport = M6LineOwnerTransport(token="a"*64, open_channel=connect,
            close_session=lambda: session_closed.append(True), continuous_ns=Clock())
        try:
            for _ in range(2):
                self.assertEqual(transport.exchange(b'{"command":"status"}'), b'{"ok":true}')
        finally:
            transport.close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(open_calls), 1)
        self.assertEqual(len(session_closed), 1)
        self.assertEqual(received, [b"M6-AUTH/1 "+b"a"*64+b"\n", b'{"command":"status"}\n']*2)

    def test_eof_oversize_crlf_trailing_line_and_deadline_close_the_channel(self):
        for response in (b"", b"x"*4097, b'{}\r\n', b'{}\n{}\n', b'delayed'):
            with self.subTest(response=response[:16]):
                clock = Clock()
                class Channel:
                    closed = False
                    def settimeout(self, value):
                        pass
                    def sendall(self, value):
                        pass
                    def recv(self, maximum):
                        if response == b'delayed':
                            clock.ns += 6_000_000_000
                            return b'{}\n'
                        return response
                    def close(self):
                        self.closed = True
                channel = Channel()
                transport = M6LineOwnerTransport(token="a"*64, open_channel=lambda timeout: channel,
                    close_session=lambda: None, continuous_ns=clock, maximum_wire_bytes=4096)
                with self.assertRaises((EOFError, ValueError, TimeoutError)):
                    transport.exchange(b'{}')
                self.assertTrue(channel.closed)
                transport.close()


if __name__ == "__main__":
    unittest.main()
