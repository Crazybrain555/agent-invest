"""Ordinary resident ownership of a pinned dynamic admission input."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import os

from disclosure_anchor.adapters.runtime.mineru_pressure_journal import PressureJournal
from disclosure_anchor.adapters.runtime.mineru_stream_activation import load_mineru_stream_activation
from disclosure_anchor.adapters.runtime.mineru_stream_pressure import StreamPressureSession
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from disclosure_anchor.application.services.mineru_stream_policy import MineruStreamPolicy, StreamAdmissionControl
from disclosure_anchor.settings import Settings


@contextmanager
def owned_mineru_stream_control(
    settings: Settings, *, expected_capacity: MineruCapacityConfig | None,
    wakeup: Callable[[], None],
) -> Iterator[StreamAdmissionControl | None]:
    activation = load_mineru_stream_activation(
        settings.disclosure_mineru_stream_pressure_config,
        expected_sha256=settings.disclosure_mineru_stream_pressure_config_sha256,
        expected_owner_uid=os.getuid(), expected_capacity=expected_capacity,
        expected_runtime_identity_sha256=settings.disclosure_mineru_runtime_bundle_identity_sha256,
    )
    if activation is None:
        yield None
        return
    api, gpu = settings.disclosure_mineru_api_url, settings.disclosure_gpu_metrics_url
    if api is None or gpu is None:
        raise ValueError("stream pressure activation requires API and GPU URLs")
    with ExitStack() as owned:
        journal = PressureJournal(settings.disclosure_runtime_root / "reports" / "stream-pressure")
        owned.callback(journal.close)
        journal({"event": "owner_start", "activation_sha256": activation.source_sha256,
                 "policy": asdict(activation.policy), "capacity_sha256": activation.binding.capacity.sha256})
        readers = StreamPressureSession(activation.binding, api_url=api, gpu_url=gpu,
                                       evidence_sink=journal, wakeup=wakeup)
        readers.start()
        owned.callback(readers.close)
        readers.wait_initial_sample()
        yield StreamAdmissionControl(MineruStreamPolicy(activation.policy), readers.cache)
