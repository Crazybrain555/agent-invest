"""Recognize PostgreSQL's interrupted relation-file open.

When ``open(2)`` of a relation fork fails with EINTR, PostgreSQL reports
``could not open file "<path>": Interrupted system call`` at ERROR with SQLSTATE
XX000, because ``errcode_for_file_access`` maps no SQLSTATE to EINTR. The
statement fails and its transaction aborts while the backend and connection
stay usable. Only that exact report is recognized. Other XX000 errors, read,
write or fsync failures, missing files, IO errors, FATAL or PANIC reports,
cancellations, timeouts and invalidated connections are not.
"""

from __future__ import annotations

import re

from psycopg import errors as pg_errors
from sqlalchemy import exc as sa_exc

# A relation fork in the default or global tablespace, spelled as relpath()
# spells it (temporary relations carry their ``t<proc>_`` prefix).
_INTERRUPTED_RELATION_FILE_OPEN = re.compile(
    r'could not open file "(?:base/[0-9]+/(?:t[0-9]+_)?[0-9]+|global/[0-9]+)'
    r'(?:_(?:fsm|vm|init))?": Interrupted system call'
)


def is_interrupted_relation_file_open(error: BaseException | None) -> bool:
    """Whether ``error`` is the driver's report of exactly that interruption."""

    if not isinstance(error, sa_exc.InternalError) or error.connection_invalidated:
        return False
    original = error.orig
    if not isinstance(original, pg_errors.InternalError_):
        return False
    diag = original.diag
    message = diag.message_primary
    return (
        diag.sqlstate == "XX000"
        and diag.severity_nonlocalized == "ERROR"
        and message is not None
        and _INTERRUPTED_RELATION_FILE_OPEN.fullmatch(message) is not None
    )


__all__ = ["is_interrupted_relation_file_open"]
