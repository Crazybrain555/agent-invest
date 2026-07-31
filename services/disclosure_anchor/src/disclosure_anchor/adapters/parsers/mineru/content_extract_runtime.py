"""Runtime-only bridge to the deployed mineru-vl-utils public client.

This module is executed by the MinerU virtualenv Python.  Imports of the
runtime-only packages remain inside ``run_request`` so application unit tests
do not need a second MinerU installation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from io import BytesIO
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
from typing import Any


def run_request(
    request: object,
    *,
    client_factory: Callable[..., Any] | None = None,
    image_open: Callable[[BytesIO], Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != {
        "server_url",
        "max_concurrency",
        "items",
    }:
        raise ValueError("content-extract request fields are not closed")
    server_url = request["server_url"]
    concurrency = request["max_concurrency"]
    items = request["items"]
    if (
        not isinstance(server_url, str)
        or not server_url
        or isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
        or not isinstance(items, list)
    ):
        raise ValueError("content-extract request identity is invalid")

    if client_factory is None or image_open is None:
        from PIL import Image
        from mineru_vl_utils import MinerUClient

        client_factory = client_factory or MinerUClient
        image_open = image_open or Image.open

    decoded: list[tuple[str, Any, str]] = []
    for raw_item in items:
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "item_id",
            "path",
            "sha256",
            "visual_type",
        }:
            raise ValueError("content-extract item fields are not closed")
        item_id = raw_item["item_id"]
        path = raw_item["path"]
        sha256 = raw_item["sha256"]
        visual_type = raw_item["visual_type"]
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(path, str)
            or not path
            or not isinstance(sha256, str)
            or visual_type not in {"image", "chart", "equation"}
        ):
            raise ValueError("content-extract item identity is invalid")
        payload = Path(path).read_bytes()
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        if actual != sha256:
            raise ValueError(f"content-extract source hash differs: {item_id}")
        with image_open(BytesIO(payload)) as opened:
            opened.load()
            decoded.append((item_id, opened.copy(), str(visual_type)))

    client = client_factory(
        backend="http-client",
        server_url=server_url,
        image_analysis=True,
        max_concurrency=concurrency,
        use_tqdm=False,
    )
    images = [item[1] for item in decoded]
    types = [item[2] for item in decoded]
    try:
        values = client.batch_content_extract(images, types=types)
    except Exception:
        # One thin fallback around the official client's own transport retry.
        # A second failure is infrastructure failure, not semantic absence.
        values = [
            client.content_extract(image, type=visual_type)
            for image, visual_type in zip(images, types, strict=True)
        ]
    if not isinstance(values, list) or len(values) != len(decoded):
        raise ValueError("content-extract output count differs")
    return {
        "mineru_vl_utils_version": version("mineru-vl-utils"),
        "outputs": [
            {
                "item_id": item_id,
                "text": None if value is None else str(value),
            }
            for (item_id, _image, _visual_type), value in zip(
                decoded,
                values,
                strict=True,
            )
        ],
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        response = run_request(request)
    except Exception as exc:
        json.dump(
            {
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            },
            sys.stdout,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return 1
    json.dump(
        response,
        sys.stdout,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
