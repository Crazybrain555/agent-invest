"""Canonical security identity helpers.

Security identity is the tuple ``(security_code, exchange)``.  Every write
and lookup must use one spelling so PostgreSQL's exact-string unique
constraint also protects semantic identity.
"""

from __future__ import annotations

import re


_LISTED_SECURITY_CODE_RE = re.compile(r"^[0-9]{6}$")
_MAINLAND_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})


def canonical_security_identity(
    security_code: str, exchange: str
) -> tuple[str, str]:
    """Strip the code and canonicalize the exchange to upper case."""

    code = security_code.strip()
    market = exchange.strip().upper()
    if not code:
        raise ValueError("security_code is required")
    if not market:
        raise ValueError("exchange is required")
    if market in _MAINLAND_EXCHANGES:
        if not _LISTED_SECURITY_CODE_RE.fullmatch(code):
            raise ValueError(
                f"security_code {code!r} must be six digits for {market}"
            )
        expected = infer_mainland_exchange(code)
        if market != expected:
            raise ValueError(
                f"security_code {code} belongs to {expected}, not {market}"
            )
    return code, market


def infer_mainland_exchange(security_code: str) -> str:
    """Infer SSE/SZSE/BSE for a six-digit listed-equity code.

    BSE's current 920 segment is checked before SSE's 9xx B-share segment;
    legacy BSE/selected-layer 4xx and 8xx codes remain accepted during the
    exchange's code migration.
    """

    code = security_code.strip()
    if not _LISTED_SECURITY_CODE_RE.fullmatch(code):
        raise ValueError(
            f"cannot infer exchange for {security_code!r}: expected 6 digits"
        )
    if code.startswith("92") or code.startswith(("4", "8")):
        return "BSE"
    if code.startswith(("6", "9")):
        return "SSE"
    if code.startswith(("0", "2", "3")):
        return "SZSE"
    raise ValueError(f"cannot infer exchange for security code {code!r}")
