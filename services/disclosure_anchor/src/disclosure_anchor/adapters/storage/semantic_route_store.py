"""Runtime cache and receipt-sidecar storage for semantic routing."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import cast

from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicationDecision,
    SemanticAdjudicatedRoute,
    SemanticProviderIdentity,
    SemanticRouteContractError,
    SemanticRouteReceiptRow,
    semantic_route_receipt_row_from_payload,
    semantic_route_receipt_row_to_payload,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationCacheEntry,
    SemanticRouteCacheError,
    SemanticRouteReceiptStoreError,
)
from disclosure_anchor.domain.ids import new_ulid


_MAX_RECEIPT_FILE_BYTES = 64 * 1024 * 1024
_MAX_GROUP_CACHE_BYTES = 2 * 1024 * 1024


class SemanticRouteReceiptStore:
    """Write/read the private per-run receipt sidecar."""

    def __init__(
        self,
        *,
        paths: FileStorePathPort,
        artifacts: ArtifactStorePort,
    ) -> None:
        self._paths = paths
        self._artifacts = artifacts

    def write(
        self,
        *,
        relpath: Path,
        rows: tuple[SemanticRouteReceiptRow, ...],
    ) -> ArtifactWriteResult:
        _validate_receipt_rows(rows)
        return self._artifacts.write_jsonl_atomic(
            relpath=relpath,
            rows=tuple(semantic_route_receipt_row_to_payload(row) for row in rows),
        )

    def read(
        self,
        *,
        relpath: Path,
        expected_hash: str,
    ) -> tuple[SemanticRouteReceiptRow, ...]:
        path = self._paths.data_path(relpath)
        try:
            stat = path.lstat()
            if path.is_symlink() or not path.is_file():
                raise SemanticRouteContractError(
                    "semantic receipt sidecar must be a regular file"
                )
            if stat.st_size > _MAX_RECEIPT_FILE_BYTES:
                raise SemanticRouteContractError("semantic receipt sidecar is too large")
            raw = path.read_bytes()
        except OSError as exc:
            raise SemanticRouteReceiptStoreError(
                "semantic receipt sidecar cannot be read",
                retryable=True,
            ) from exc
        actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            raise SemanticRouteContractError(
                "semantic receipt sidecar hash differs from processing run"
            )
        rows: list[SemanticRouteReceiptRow] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                raise SemanticRouteContractError(
                    f"semantic receipt sidecar has an empty line: {line_number}"
                )
            try:
                payload = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SemanticRouteContractError(
                    f"semantic receipt sidecar line is invalid: {line_number}"
                ) from exc
            rows.append(semantic_route_receipt_row_from_payload(payload))
        result = tuple(rows)
        _validate_receipt_rows(result)
        return result


class SemanticRouteFileCache:
    """Content-addressed per-Unit adjudication cache under the runtime root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def get(self, cache_key: str) -> SemanticAdjudicationDecision | None:
        path = self._path(cache_key)
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return None
        if path.is_symlink() or not path.is_file() or stat.st_size > 128 * 1024:
            raise SemanticRouteContractError("semantic route cache entry is unsafe")
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SemanticRouteContractError("semantic route cache entry is invalid") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SemanticRouteContractError("semantic route cache entry is invalid") from exc
        return _decision_from_cache_payload(payload, cache_key=cache_key)

    def put(
        self,
        cache_key: str,
        decision: SemanticAdjudicationDecision,
    ) -> None:
        path = self._path(cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_key": cache_key,
            "contract_version": "semantic_route_cache.v1",
            "decision": _decision_to_payload(decision),
        }
        raw = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        tmp = path.with_name(f".{path.name}.{new_ulid()}.tmp")
        try:
            with tmp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _path(self, cache_key: str) -> Path:
        if not cache_key.startswith("sha256:") or len(cache_key) != 71:
            raise SemanticRouteContractError("semantic cache key is invalid")
        digest = cache_key.removeprefix("sha256:")
        if any(char not in "0123456789abcdef" for char in digest):
            raise SemanticRouteContractError("semantic cache key is invalid")
        return self._root / digest[:2] / f"{digest}.json"


class SemanticRouteGroupFileCache:
    """Atomic group-level v2 cache with corruption quarantine."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def get(self, cache_key: str) -> SemanticAdjudicationCacheEntry | None:
        path = self._path(cache_key)
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SemanticRouteCacheError(
                "semantic group cache cannot be inspected",
                retryable=True,
            ) from exc
        if path.is_symlink() or not path.is_file() or stat.st_size > _MAX_GROUP_CACHE_BYTES:
            raise SemanticRouteContractError("semantic group cache entry is unsafe")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SemanticRouteCacheError(
                "semantic group cache cannot be read",
                retryable=True,
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._quarantine(path)
            return None
        if not isinstance(payload, dict):
            self._quarantine(path)
            return None
        if payload.get("contract_version") != "semantic_route_cache.v2":
            raise SemanticRouteContractError("semantic group cache version drifted")
        if payload.get("cache_key") != cache_key:
            raise SemanticRouteContractError("semantic group cache key drifted")
        try:
            return _group_cache_entry_from_payload(payload)
        except (KeyError, TypeError, ValueError, SemanticRouteContractError):
            self._quarantine(path)
            return None

    def put(self, entry: SemanticAdjudicationCacheEntry) -> None:
        path = self._path(entry.cache_key)
        payload = _group_cache_entry_to_payload(entry)
        raw = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = self.get(entry.cache_key)
            if existing is not None:
                if existing != entry:
                    raise SemanticRouteContractError(
                        "semantic group cache is nondeterministic"
                    )
                return
            tmp = path.with_name(f".{path.name}.{new_ulid()}.tmp")
            try:
                with tmp.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    existing = self.get(entry.cache_key)
                    if existing != entry:
                        raise SemanticRouteContractError(
                            "semantic group cache is nondeterministic"
                        )
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if tmp.exists():
                    tmp.unlink()
        except SemanticRouteContractError:
            raise
        except OSError as exc:
            raise SemanticRouteCacheError(
                "semantic group cache cannot be written",
                retryable=True,
            ) from exc

    def _quarantine(self, path: Path) -> None:
        target = path.with_name(f"{path.name}.corrupt.{new_ulid()}")
        try:
            os.replace(path, target)
        except OSError as exc:
            raise SemanticRouteCacheError(
                "semantic group cache corruption cannot be quarantined",
                retryable=True,
            ) from exc

    def _path(self, cache_key: str) -> Path:
        if not cache_key.startswith("sha256:") or len(cache_key) != 71:
            raise SemanticRouteContractError("semantic group cache key is invalid")
        digest = cache_key.removeprefix("sha256:")
        if any(char not in "0123456789abcdef" for char in digest):
            raise SemanticRouteContractError("semantic group cache key is invalid")
        return self._root / digest[:2] / f"{digest}.json"


def _group_cache_entry_to_payload(
    entry: SemanticAdjudicationCacheEntry,
) -> dict[str, object]:
    identity = entry.provider
    return {
        "cache_key": entry.cache_key,
        "contract_version": "semantic_route_cache.v2",
        "decisions": [_decision_to_payload(item) for item in entry.decisions],
        "group_hash": entry.group_hash,
        "provider": {
            "adapter_kind": identity.adapter_kind,
            "adapter_version": identity.adapter_version,
            "canonical_model": identity.canonical_model,
            "inference_profile": identity.inference_profile,
            "output_schema_sha256": identity.output_schema_sha256,
            "output_schema_version": identity.output_schema_version,
            "prompt_sha256": identity.prompt_sha256,
            "prompt_version": identity.prompt_version,
            "provider": identity.provider,
            "provider_id": identity.provider_id,
        },
        "response_sha256": entry.response_sha256,
    }


def _group_cache_entry_from_payload(payload: dict[str, object]) -> SemanticAdjudicationCacheEntry:
    expected = {
        "cache_key",
        "contract_version",
        "decisions",
        "group_hash",
        "provider",
        "response_sha256",
    }
    if set(payload) != expected:
        raise SemanticRouteContractError("semantic group cache fields are not closed")
    provider = payload["provider"]
    if not isinstance(provider, dict):
        raise SemanticRouteContractError("semantic group cache provider is invalid")
    identity_fields = {
        "adapter_kind",
        "adapter_version",
        "canonical_model",
        "inference_profile",
        "output_schema_sha256",
        "output_schema_version",
        "prompt_sha256",
        "prompt_version",
        "provider",
        "provider_id",
    }
    if set(provider) != identity_fields or any(
        not isinstance(provider[field], str) for field in identity_fields
    ):
        raise SemanticRouteContractError("semantic group cache provider is invalid")
    raw_decisions = payload["decisions"]
    if not isinstance(raw_decisions, list):
        raise SemanticRouteContractError("semantic group cache decisions are invalid")
    decisions = tuple(
        _decision_from_cache_payload(
            {
                "cache_key": payload["cache_key"],
                "contract_version": "semantic_route_cache.v1",
                "decision": item,
            },
            cache_key=cast(str, payload["cache_key"]),
        )
        for item in raw_decisions
    )
    for field in ("cache_key", "group_hash", "response_sha256"):
        if not isinstance(payload[field], str):
            raise SemanticRouteContractError("semantic group cache identity is invalid")
    return SemanticAdjudicationCacheEntry(
        cache_key=cast(str, payload["cache_key"]),
        group_hash=cast(str, payload["group_hash"]),
        provider=SemanticProviderIdentity(
            **{field: cast(str, provider[field]) for field in identity_fields}
        ),
        decisions=decisions,
        response_sha256=cast(str, payload["response_sha256"]),
    )


def _validate_receipt_rows(rows: tuple[SemanticRouteReceiptRow, ...]) -> None:
    if tuple(row.order_index for row in rows) != tuple(range(1, len(rows) + 1)):
        raise SemanticRouteContractError(
            "semantic receipt rows must be contiguous in Unit order"
        )
    asset_ids = [row.asset_id for row in rows]
    if len(asset_ids) != len(set(asset_ids)):
        raise SemanticRouteContractError("semantic receipt rows repeat an asset")


def _decision_to_payload(decision: SemanticAdjudicationDecision) -> dict[str, object]:
    return {
        "routes": [
            {"key": route.key, "support_ids": list(route.support_ids)}
            for route in decision.routes
        ],
        "unit_index": decision.unit_index,
    }


def _decision_from_cache_payload(
    payload: object,
    *,
    cache_key: str,
) -> SemanticAdjudicationDecision:
    if not isinstance(payload, dict) or set(payload) != {
        "cache_key",
        "contract_version",
        "decision",
    }:
        raise SemanticRouteContractError("semantic route cache fields are not closed")
    if payload["cache_key"] != cache_key or payload["contract_version"] != "semantic_route_cache.v1":
        raise SemanticRouteContractError("semantic route cache identity drifted")
    raw = payload["decision"]
    if not isinstance(raw, dict) or set(raw) != {"routes", "unit_index"}:
        raise SemanticRouteContractError("semantic route cached decision is invalid")
    unit_index = raw["unit_index"]
    routes = raw["routes"]
    if type(unit_index) is not int or not isinstance(routes, list):
        raise SemanticRouteContractError("semantic route cached decision is invalid")
    decoded: list[SemanticAdjudicatedRoute] = []
    for item in routes:
        if not isinstance(item, dict) or set(item) != {"key", "support_ids"}:
            raise SemanticRouteContractError("semantic route cached route is invalid")
        key = item["key"]
        support_ids = item["support_ids"]
        if not isinstance(key, str) or not isinstance(support_ids, list) or any(
            not isinstance(value, str) for value in support_ids
        ):
            raise SemanticRouteContractError("semantic route cached route is invalid")
        decoded.append(
            SemanticAdjudicatedRoute(key=key, support_ids=tuple(support_ids))
        )
    return SemanticAdjudicationDecision(unit_index=unit_index, routes=tuple(decoded))


__all__ = [
    "SemanticRouteFileCache",
    "SemanticRouteGroupFileCache",
    "SemanticRouteReceiptStore",
]
