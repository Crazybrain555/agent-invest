"""Classify one missing derived file separately from a data-store outage."""

from __future__ import annotations

from pathlib import Path

from disclosure_anchor.application.ports.file_store import FileStorePathPort


class DataFileMissingError(OSError):
    """The configured data root is online, but this one file is absent."""


class DataStoreReadError(OSError):
    """The configured data root or a file on it cannot be read safely."""


def read_data_file_bytes(
    path_builder: FileStorePathPort,
    relpath: Path,
) -> bytes:
    """Read a data file while preserving item-vs-infrastructure semantics."""

    try:
        path = path_builder.data_path(relpath)
        return path.read_bytes()
    except FileNotFoundError as exc:
        try:
            data_root_online = path_builder.data_path(Path()).is_dir()
        except OSError:
            data_root_online = False
        if data_root_online:
            raise DataFileMissingError(
                f"data file is missing: {relpath}"
            ) from exc
        raise DataStoreReadError(
            f"data root is unavailable while reading: {relpath}"
        ) from exc
    except OSError as exc:
        raise DataStoreReadError(
            f"data file cannot be read: {relpath}: {exc}"
        ) from exc
