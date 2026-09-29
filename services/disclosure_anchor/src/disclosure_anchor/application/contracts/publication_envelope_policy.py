"""One fixed byte envelope for every private record of a V4 whole-document publication.

A release carries exactly one :data:`PUBLICATION_ENVELOPE_POLICY_V1`, fixed in
source.  Every writer and every reader of the publication chain takes its limit
from that value: the canonical request (and the request text embedded in the
preparation), each pre-ID Unit record and final Unit/lineage row hash input, the
preparation, the readiness manifest and its Unit-binding aggregates, the
transaction-P winner and its outbox rows, and the Unit snapshot and semantic
receipt files.  What one layer may write, the next layer can read back.  No
setting, environment value or fallback changes it; another domain is another
release.

A budget bounds encoded bytes only.  It is neither a memory nor a time bound:
the heavy publication stage still holds the whole request, its Python objects
and several of these encodings at once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping


PUBLICATION_ENVELOPE_POLICY_V1_CONTRACT = "publication-envelope-policy.v1"
MIB = 1024 * 1024
# Mirror of the applied 0057 CHECK on atomic_publication_winner_v4.winner_bytes.
# A larger winner needs a new migration; this value is never a policy knob.
ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES = 8 * MIB

# Every canonical encoding of the chain is classified under one record kind,
# and every record kind is bounded by exactly one budget of the policy.  A
# component of a record (a hash input, an embedded JSON text) is checked
# against its record's budget with a lower-bound byte count.
PUBLICATION_RECORD_BUDGETS: Mapping[str, str] = MappingProxyType(
    {
        "request": "request_bytes",
        "previous_active_inventory": "request_bytes",
        "unit": "unit_bytes",
        "unit_row": "unit_bytes",
        "preparation": "preparation_bytes",
        "readiness": "readiness_bytes",
        "unit_bindings": "readiness_bytes",
        "winner": "winner_bytes",
        "snapshot": "snapshot_bytes",
        "semantic": "semantic_bytes",
    }
)
# ``exact`` is the whole record; ``lower_bound`` a count the record is at least
# (a component or a prefix); ``upper_bound`` a conservative projection of a
# record whose final bytes do not exist yet (the transaction-P winner).
PUBLICATION_BYTE_BOUNDS = frozenset({"exact", "lower_bound", "upper_bound"})

_BUDGET_FIELDS = (
    "request_bytes",
    "unit_bytes",
    "preparation_bytes",
    "readiness_bytes",
    "winner_bytes",
    "snapshot_bytes",
    "semantic_bytes",
)
_MAX_BUDGET = 1 << 30
_MAX_COUNT = (1 << 63) - 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def canonical_publication_json(value: object) -> bytes:
    """Encode with the options every canonical publication record has always used."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PublicationCapacityFactV1:
    """Content-free fact of one record this release's envelope refuses.

    An ``exact`` or ``lower_bound`` count shows the record is larger than its
    limit. An ``upper_bound`` count is a conservative projection of a record
    not written yet: its refusal does not show that the record's actual bytes
    would exceed the limit.
    """

    record_kind: str
    bound: str
    byte_count: int
    limit: int
    policy_identity: str

    def __post_init__(self) -> None:
        if self.record_kind not in PUBLICATION_RECORD_BUDGETS:
            raise ValueError("publication record kind is outside the closed vocabulary")
        if self.bound not in PUBLICATION_BYTE_BOUNDS:
            raise ValueError("publication byte bound is outside the closed vocabulary")
        for value in (self.byte_count, self.limit):
            if type(value) is not int or not 0 <= value <= _MAX_COUNT:
                raise ValueError("publication capacity counts must be non-negative integers")
        if self.byte_count <= self.limit:
            raise ValueError("a publication capacity fact must exceed its limit")
        if not isinstance(self.policy_identity, str) or _SHA256.fullmatch(self.policy_identity) is None:
            raise ValueError("publication policy identity is not canonical")


@dataclass(frozen=True, slots=True)
class PublicationEnvelopePolicyV1:
    """Per-record byte budgets of one release; see the module docstring."""

    request_bytes: int
    unit_bytes: int
    preparation_bytes: int
    readiness_bytes: int
    winner_bytes: int
    snapshot_bytes: int
    semantic_bytes: int
    contract_version: str = PUBLICATION_ENVELOPE_POLICY_V1_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != PUBLICATION_ENVELOPE_POLICY_V1_CONTRACT:
            raise ValueError("publication envelope policy contract is unsupported")
        for name in _BUDGET_FIELDS:
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= _MAX_BUDGET:
                raise ValueError(f"publication envelope {name} is outside its range")
        if self.unit_bytes > self.request_bytes:
            raise ValueError("a Unit record cannot be larger than its request")
        if self.winner_bytes > ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES:
            raise ValueError("the winner budget exceeds the applied database CHECK")

    def limit(self, record_kind: str) -> int:
        try:
            name = PUBLICATION_RECORD_BUDGETS[record_kind]
        except KeyError:
            raise ValueError("publication record kind is outside the closed vocabulary") from None
        value: int = getattr(self, name)
        return value

    def exceeded(
        self,
        record_kind: str,
        byte_count: int,
        *,
        bound: str = "exact",
    ) -> PublicationCapacityFactV1 | None:
        """Return the capacity fact when ``byte_count`` is over the record's limit.

        A lower bound within the limit proves nothing: the exact record is
        still checked where it is encoded. An upper bound over the limit
        refuses conservatively (see :class:`PublicationCapacityFactV1`).
        """

        limit = self.limit(record_kind)
        if type(byte_count) is not int or byte_count < 0:
            raise ValueError("publication record byte count must be a non-negative integer")
        if byte_count <= limit:
            return None
        return PublicationCapacityFactV1(
            record_kind=record_kind,
            bound=bound,
            byte_count=byte_count,
            limit=limit,
            policy_identity=self.identity,
        )

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_publication_json(asdict(self))

    @property
    def identity(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes).hexdigest()


# Sizing: a request may be one Unit of nearly its whole size, so a Unit record
# has the request's budget.  The preparation embeds the request as JSON text (at
# most twice its bytes, when every character needs an escape) beside Unit
# bindings that readiness also bounds; the snapshot re-encodes each payload
# with json's default separators (at most one and a half times its canonical
# text) beside per-row fields; the semantic file is a subset of the request.
# Each record is still checked against its own budget, and the winner, bounded
# by the database CHECK, is admitted only through its conservative pre-write
# bound: a request within its budget can still be refused by another record.
PUBLICATION_ENVELOPE_POLICY_V1 = PublicationEnvelopePolicyV1(
    request_bytes=64 * MIB,
    unit_bytes=64 * MIB,
    preparation_bytes=160 * MIB,
    readiness_bytes=8 * MIB,
    winner_bytes=ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES,
    snapshot_bytes=128 * MIB,
    semantic_bytes=64 * MIB,
)


__all__ = [
    "ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES",
    "PUBLICATION_BYTE_BOUNDS",
    "PUBLICATION_ENVELOPE_POLICY_V1",
    "PUBLICATION_ENVELOPE_POLICY_V1_CONTRACT",
    "PUBLICATION_RECORD_BUDGETS",
    "PublicationCapacityFactV1",
    "PublicationEnvelopePolicyV1",
    "canonical_publication_json",
]
