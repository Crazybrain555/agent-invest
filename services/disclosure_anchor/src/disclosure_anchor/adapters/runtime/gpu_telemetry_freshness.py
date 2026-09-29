"""Same-host freshness semantics for pinned nvidia-smi exporter responses.

The exporter stamps each successful collection with ``LastSuccess.Unix()``
and its Go HTTP server stamps each response ``Date`` from the same host clock;
both carry whole seconds. Their difference bounds a sample's age when its
response was generated; the reader adds its own monotonic request-to-receipt
time. The exporter host and the reading host are not clock-synchronized, so
neither value is ever compared with a local wall clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import math
import re


NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS = 30.0
EXPORTER_DATE_HEADER = "Date"

_IMF_FIXDATE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([0-9]{2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) GMT"
)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class GpuTelemetryUnavailable(ValueError):
    """A valid exporter response lacks a current successful observation."""


class GpuSampleStaleError(GpuTelemetryUnavailable):
    """The last successful observation is too old for a strict sampler."""


class GpuCollectionUnavailableError(GpuTelemetryUnavailable):
    """The exporter reports that its latest collection did not succeed."""


class GpuSampleClockUnorderedError(GpuTelemetryUnavailable):
    """The exporter host clock stepped back between collection and response."""


class GpuClockEvidenceError(ValueError):
    """A response lacks exactly one well-formed exporter clock value."""


def exporter_response_date_seconds(values: Sequence[str]) -> int:
    """Return the Unix seconds of exactly one RFC 9110 IMF-fixdate ``Date``.

    The pinned Go server always sends one. Absent, repeated, obsolete-format or
    impossible dates fail closed; nothing substitutes a local reading time.
    """

    if isinstance(values, (str, bytes)) or len(values) != 1 or not isinstance(values[0], str):
        raise GpuClockEvidenceError("exporter response must carry exactly one HTTP Date")
    match = _IMF_FIXDATE.fullmatch(values[0])
    if match is None:
        raise GpuClockEvidenceError("exporter HTTP Date is not an IMF-fixdate")
    weekday, day, month, year, hour, minute, second = match.groups()
    try:
        moment = datetime(
            int(year), _MONTHS.index(month) + 1, int(day),
            int(hour), int(minute), int(second), tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise GpuClockEvidenceError("exporter HTTP Date is not a real instant") from exc
    if _WEEKDAYS[moment.weekday()] != weekday:
        raise GpuClockEvidenceError("exporter HTTP Date weekday is inconsistent")
    return int(moment.timestamp())


def exporter_sample_age_bound_seconds(
    *,
    response_date_seconds: int,
    success_timestamp: float,
    transport_elapsed_seconds: float,
) -> float:
    """Return a strict upper bound of the sample's age once fully received.

    ``Date`` floors the response instant and ``Unix()`` floors the collection
    instant, so the age at response time is below ``date + 1 - success``. The
    Date is stamped before the body finishes arriving, so the caller's local
    monotonic time from request start to complete receipt is added. A
    non-positive same-host part means the response instant precedes its own
    collection on one host clock: that clock stepped back, and this response
    cannot bound its sample's age.
    """

    if (
        isinstance(transport_elapsed_seconds, bool)
        or not isinstance(transport_elapsed_seconds, (int, float))
        or not math.isfinite(transport_elapsed_seconds)
        or transport_elapsed_seconds < 0
    ):
        raise ValueError("local exporter transport elapsed time is invalid")
    if (
        isinstance(response_date_seconds, bool)
        or not isinstance(response_date_seconds, int)
        or response_date_seconds <= 0
    ):
        raise GpuClockEvidenceError("exporter HTTP Date is invalid")
    if (
        isinstance(success_timestamp, bool)
        or not isinstance(success_timestamp, (int, float))
        or not math.isfinite(success_timestamp)
        or success_timestamp <= 0
    ):
        raise GpuClockEvidenceError("nvidia-smi exporter success timestamp is invalid")
    bound = response_date_seconds + 1 - success_timestamp
    if bound <= 0:
        raise GpuSampleClockUnorderedError(
            "nvidia-smi exporter response predates its own collection"
        )
    return bound + transport_elapsed_seconds
