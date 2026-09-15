"""Optional scalar stage observation attached to a bounded stage guard.

Observation is measurement only. It never grants or revokes execution
permission, never carries document text, paths or credentials, and a missing
observer means zero calls. Guards expose ``note``; call sites reach it only
through :func:`note_stage`, so guards and doubles without the method are
untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol


_MAX_SCALAR_TEXT = 256


@dataclass(frozen=True, slots=True)
class StageNote:
    """One scalar observation bound to an attempt/lane and a monotonic instant."""

    attempt_id: str | None
    lane: str | None
    kind: str
    monotonic_ns: int
    scalars: tuple[tuple[str, int | str | None], ...]

    def __post_init__(self) -> None:
        if not self.kind or not self.kind.isidentifier():
            raise ValueError("stage note kind must be an identifier")
        if type(self.monotonic_ns) is not int or self.monotonic_ns < 0:
            raise ValueError("stage note instant must be a nonnegative integer")
        seen: set[str] = set()
        for key, value in self.scalars:
            if not key or not key.isidentifier() or key in seen:
                raise ValueError("stage note scalar keys must be unique identifiers")
            seen.add(key)
            if value is not None and type(value) not in (int, str):
                raise ValueError("stage note scalars must be int, str or None")
            if isinstance(value, str) and len(value) > _MAX_SCALAR_TEXT:
                raise ValueError("stage note scalar text exceeds its bound")


class StageObserverPort(Protocol):
    def note(self, record: StageNote) -> None:
        """Record one observation; must not raise into business code."""
        ...

    def record_failure(self, error: BaseException) -> None:
        """Count an observation failure sticky so the measurement is not claimed complete."""
        ...


def note_stage(guard: object, kind: str, **scalars: int | str | None) -> None:
    """Emit a scalar note when the guard carries an observer; otherwise do nothing."""

    note = getattr(guard, "note", None)
    if callable(note):
        note(kind, **scalars)


_SEMANTIC_GROUP: ContextVar[str | None] = ContextVar("disclosure_semantic_group", default=None)


def current_semantic_group() -> str | None:
    """Return the group hash the executor is currently adjudicating on this thread."""

    return _SEMANTIC_GROUP.get()


@contextmanager
def semantic_group_scope(group_hash: str) -> Iterator[None]:
    token = _SEMANTIC_GROUP.set(group_hash)
    try:
        yield
    finally:
        _SEMANTIC_GROUP.reset(token)


__all__ = [
    "StageNote",
    "StageObserverPort",
    "current_semantic_group",
    "note_stage",
    "semantic_group_scope",
]
