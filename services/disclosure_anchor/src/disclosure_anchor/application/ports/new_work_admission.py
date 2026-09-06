"""Readiness affects new work, independently of already-owned execution."""


class NewWorkAdmissionUnavailable(RuntimeError):
    """A typed availability deferral, never an ownership or identity failure."""


__all__ = ["NewWorkAdmissionUnavailable"]
