"""Bounded freshness semantics for cross-host GPU exporter snapshots."""

from __future__ import annotations

import math


NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS = 30.0
NVIDIA_SMI_MAX_FUTURE_SKEW_SECONDS = 1.0


def nvidia_smi_sample_age_seconds(
    *,
    now_timestamp: float,
    success_timestamp: float,
) -> float:
    """Return a nonnegative age while rejecting material clock drift/staleness."""

    if not math.isfinite(now_timestamp) or not math.isfinite(success_timestamp):
        raise ValueError("nvidia-smi exporter timestamp is not finite")
    sample_age = now_timestamp - success_timestamp
    if sample_age < -NVIDIA_SMI_MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("nvidia-smi exporter timestamp is too far in the future")
    if sample_age > NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS:
        raise ValueError("nvidia-smi exporter sample is stale")
    return max(0.0, sample_age)
