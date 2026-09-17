"""Pure finite budgets for the one M6 launcher transport and the sampling window that must cover it.

The Mac launcher transport (one ``BoundedOwnerCommand`` running
``run_mineru_m6_owner_host.ps1 -Run``) is spawned BEFORE the native owner exists,
so its absolute deadline is fixed from the local spawn instant while the business
window ``[T0, T0 + planned + grace]`` is fixed on the owner clock from a later
instant. A correct transport deadline therefore adds the bounded pre-T0 delay
and the bounded post-close tail to the business window instead of capping the
sum at the default command ceiling. Nothing here renews a deadline, changes the
owner's planned/grace/max_close, the 30 s lease, admission, publication or ACK
semantics, or bounds the production worker loop (which is unbounded and
liveness-supervised). Every term is an existing code or launcher bound.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from disclosure_anchor.application.services.resident_measurement_policy import FINITE_COMMAND_MAX_SECONDS

DEFAULT_COMMAND_CEILING_SECONDS = 7200
# The one finite-diagnostic ceiling (R22 vector 8500/8580/8590/8600); the
# measurement policy is its source, this name is the launcher/owner consumer.
EXTENDED_COMMAND_CEILING_SECONDS = FINITE_COMMAND_MAX_SECONDS
OWNER_HOST_CEILING_SECONDS = 7200
# The composition root polls the launcher this long past its own deadline.
FINISH_POLL_ALLOWANCE_SECONDS = 5.0


def _finite_nonnegative(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return float(value)


def _positive_int(value: int, *, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class LaunchTransportBudget:
    """The launcher transport deadline decomposed into its bounded terms (seconds).

    ``pre_t0_seconds``: local spawn → READY bound the composition root enforces
    (``ready_wait_seconds + ssh_overhead_seconds``); the owner's T0 precedes READY.
    ``business_seconds``: T0 → original max_close (``planned + close_grace``).
    ``post_close_seconds``: the launcher's own ``ExitWaitExtraSeconds`` allowance
    for the owner to exit after max_close, its post-exit drain/record allowance
    and the ssh teardown overhead.
    """

    planned_seconds: int
    close_grace_seconds: int
    ready_wait_seconds: int
    ssh_overhead_seconds: float
    exit_wait_extra_seconds: int
    launcher_drain_seconds: float

    @property
    def pre_t0_seconds(self) -> float:
        return self.ready_wait_seconds + self.ssh_overhead_seconds

    @property
    def business_seconds(self) -> int:
        return self.planned_seconds + self.close_grace_seconds

    @property
    def post_close_seconds(self) -> float:
        return self.exit_wait_extra_seconds + self.launcher_drain_seconds + self.ssh_overhead_seconds

    @property
    def timeout_seconds(self) -> float:
        return self.pre_t0_seconds + self.business_seconds + self.post_close_seconds

    @property
    def lifetime_ceiling_seconds(self) -> int:
        """The default command ceiling when it suffices; the extended one only for this transport."""
        return (DEFAULT_COMMAND_CEILING_SECONDS if self.timeout_seconds <= DEFAULT_COMMAND_CEILING_SECONDS
                else EXTENDED_COMMAND_CEILING_SECONDS)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "planned_seconds": self.planned_seconds, "close_grace_seconds": self.close_grace_seconds,
            "ready_wait_seconds": self.ready_wait_seconds, "ssh_overhead_seconds": self.ssh_overhead_seconds,
            "exit_wait_extra_seconds": self.exit_wait_extra_seconds, "launcher_drain_seconds": self.launcher_drain_seconds,
            "pre_t0_seconds": self.pre_t0_seconds, "business_seconds": self.business_seconds,
            "post_close_seconds": self.post_close_seconds, "timeout_seconds": self.timeout_seconds,
            "lifetime_ceiling_seconds": self.lifetime_ceiling_seconds,
        }


def launch_transport_budget(
    *, planned_seconds: int, close_grace_seconds: int, ready_wait_seconds: int,
    ssh_overhead_seconds: float, exit_wait_extra_seconds: int, launcher_drain_seconds: float,
) -> LaunchTransportBudget:
    """Derive the one positive finite launcher deadline; refuse what no ceiling can hold."""
    budget = LaunchTransportBudget(
        planned_seconds=_positive_int(planned_seconds, label="planned_seconds"),
        close_grace_seconds=_positive_int(close_grace_seconds, label="close_grace_seconds"),
        ready_wait_seconds=_positive_int(ready_wait_seconds, label="ready_wait_seconds"),
        ssh_overhead_seconds=_finite_nonnegative(ssh_overhead_seconds, label="ssh_overhead_seconds"),
        exit_wait_extra_seconds=_positive_int(exit_wait_extra_seconds, label="exit_wait_extra_seconds"),
        launcher_drain_seconds=_finite_nonnegative(launcher_drain_seconds, label="launcher_drain_seconds"),
    )
    if budget.business_seconds > OWNER_HOST_CEILING_SECONDS:
        raise ValueError("planned plus grace seconds exceed the owner host ceiling")
    if budget.timeout_seconds > EXTENDED_COMMAND_CEILING_SECONDS:
        raise ValueError("launcher transport budget exceeds the extended command ceiling")
    return budget


def transport_headroom_seconds(
    *, launcher_deadline_ns: int, now_ns: int, remaining_close_seconds: float, post_close_seconds: float,
) -> float:
    """Seconds the fixed transport deadline still has beyond the owner's legal close plus its exit tail.

    Evaluated once T0 is known (after READY/open) with the owner-clock remaining
    time to max_close; a negative value means the transport would end a legal
    run early and admission must not start.
    """
    if type(launcher_deadline_ns) is not int or type(now_ns) is not int or now_ns > launcher_deadline_ns + 10**12:
        raise ValueError("launcher deadline or local instant invalid")
    transport_remaining = (launcher_deadline_ns - now_ns) / 1_000_000_000
    return transport_remaining - _finite_nonnegative(remaining_close_seconds, label="remaining_close_seconds") \
        - _finite_nonnegative(post_close_seconds, label="post_close_seconds")


def finish_wait_seconds(*, launcher_deadline_ns: int, now_ns: int, allowance_seconds: float = FINISH_POLL_ALLOWANCE_SECONDS) -> float:
    """How long to keep polling the launcher for its natural end: its own absolute deadline plus a small allowance.

    The transport deadline already contains the post-close tail, so no grace or
    extra wait is added on top of it (that would count the same deadline twice).
    """
    if type(launcher_deadline_ns) is not int or type(now_ns) is not int:
        raise ValueError("launcher deadline or local instant invalid")
    return max(0.0, (launcher_deadline_ns - now_ns) / 1_000_000_000) + _finite_nonnegative(allowance_seconds, label="allowance_seconds")


@dataclass(frozen=True, slots=True)
class SamplingCoverageTerms:
    """Worst-case bounded durations one finite telemetry window must hold to cover a whole campaign (seconds).

    Coverage is entry → summary: the composition root's prepare/stage bounds
    before the launcher spawns, the launcher transport deadline, the bounded
    evidence read-back after the launcher ended, and one host_slow nominal
    period on each edge. There is no shorter policy; a window that cannot hold
    these terms does not cover the campaign.
    """

    entry_to_spawn_seconds: float          # prepare + stage command bounds before the launcher spawns
    owner_transport_seconds: float         # the launcher transport deadline (pre-T0 + business + post-close)
    post_transport_seconds: float          # bounded evidence read-back after the launcher ended (Mac-only sftp/ssh)
    host_period_seconds: float             # one host_slow nominal period
    finish_poll_seconds: float = FINISH_POLL_ALLOWANCE_SECONDS   # launcher finish poll past its own deadline
    edge_periods: int = 2                  # source edge reserve on each side, in host periods (R22: two)

    def __post_init__(self) -> None:
        for label in ("entry_to_spawn_seconds", "owner_transport_seconds", "post_transport_seconds", "host_period_seconds", "finish_poll_seconds"):
            _finite_nonnegative(getattr(self, label), label=label)
        if type(self.edge_periods) is not int or self.edge_periods < 1:
            raise ValueError("edge_periods must be a positive integer")

    @property
    def edge_reserve_seconds(self) -> float:
        return self.edge_periods * self.host_period_seconds

    @property
    def required_seconds(self) -> float:
        """Entry → summary: prepare/stage, transport, finish poll, read-back, plus the edge reserve on each side."""
        return (self.entry_to_spawn_seconds + self.owner_transport_seconds + self.finish_poll_seconds
                + self.post_transport_seconds + 2 * self.edge_reserve_seconds)

    def prelude_allowed(self, *, sampling_seconds: float) -> float:
        """Seconds left for telemetry start → first trusted frames → campaign entry; negative means no fit."""
        return _finite_nonnegative(sampling_seconds, label="sampling_seconds") - self.required_seconds


__all__ = [
    "DEFAULT_COMMAND_CEILING_SECONDS", "EXTENDED_COMMAND_CEILING_SECONDS", "FINISH_POLL_ALLOWANCE_SECONDS", "OWNER_HOST_CEILING_SECONDS",
    "LaunchTransportBudget", "SamplingCoverageTerms", "finish_wait_seconds", "launch_transport_budget",
    "transport_headroom_seconds",
]
