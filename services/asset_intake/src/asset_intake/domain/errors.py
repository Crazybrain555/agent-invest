"""Public error model (protocol §3.11).

Read-side error codes for the future API/MCP surface; the CLI and registrar
raise IntakeError today so codes stay stable when the HTTP layer arrives.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # 请求对象仅有 raw 登记、尚未完成载体规范化(L1 读侧语义,§3.11)
    L1_PROCESSING_REQUIRED = "L1_PROCESSING_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
    GONE_SUPERSEDED = "GONE_SUPERSEDED"


class IntakeError(Exception):
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
