"""asset:// URI rules (protocol §2.3).

Hard rules: the URI carries immutable identity only (kind + stable ID); non-identity fields never enter
the URI; '/' is the hierarchy separator only, every segment is URL-encoded.

    asset://{service}/v{n}/{asset_kind}/{stable_id}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

from envelope_kernel.kinds import AssetKind

SCHEME = "asset"
_VERSION_RE = re.compile(r"^v(\d+)$")


@dataclass(frozen=True)
class AssetUri:
    service: str
    version: int
    asset_kind: AssetKind
    stable_id: str

    def __str__(self) -> str:
        return build_asset_uri(self.service, self.version, self.asset_kind, self.stable_id)


def build_asset_uri(service: str, version: int, asset_kind: AssetKind, stable_id: str) -> str:
    if not service:
        raise ValueError("service must be a non-empty string")
    if version < 1:
        raise ValueError("version must be >= 1")
    if not stable_id:
        raise ValueError("stable_id must be a non-empty string")
    return (
        f"{SCHEME}://{quote(service, safe='')}/v{version}"
        f"/{quote(asset_kind.value, safe='')}/{quote(stable_id, safe='')}"
    )


def parse_asset_uri(uri: str) -> AssetUri:
    prefix = f"{SCHEME}://"
    if not uri.startswith(prefix):
        raise ValueError(f"not an {SCHEME}:// URI: {uri!r}")
    segments = uri[len(prefix) :].split("/")
    if len(segments) != 4 or not all(segments):
        raise ValueError(
            f"malformed {SCHEME}:// URI (expected {SCHEME}://service/vN/kind/stable_id): {uri!r}"
        )
    raw_service, raw_version, raw_kind, raw_id = segments
    version_match = _VERSION_RE.match(raw_version)
    if version_match is None:
        raise ValueError(f"malformed version segment {raw_version!r} in {uri!r}")
    kind_value = unquote(raw_kind)
    try:
        asset_kind = AssetKind(kind_value)
    except ValueError:
        raise ValueError(f"unknown asset_kind {kind_value!r} in {uri!r}") from None
    return AssetUri(
        service=unquote(raw_service),
        version=int(version_match.group(1)),
        asset_kind=asset_kind,
        stable_id=unquote(raw_id),
    )
