# -*- coding: utf-8 -*-
"""
Cross-platform filesystem helpers.

These utilities make sure paths stored with Windows drive prefixes
(`F:/...`) continue to work inside WSL by mapping them onto `/mnt/<drive>`.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


def normalize_storage_path(path_like: str | Path, *, base_dir: str | Path | None = None) -> Path:
    """
    Normalize dataset/config paths so they work across Windows + WSL.

    Parameters
    ----------
    path_like:
        Original path taken from schemas/configs. May already be a Path object.
    base_dir:
        Optional directory used to anchor relative paths (e.g., dataset root).

    Returns
    -------
    Path
        A pathlib.Path pointing to the appropriate location for the current OS.
    """
    if path_like is None:
        raise ValueError("path_like is required")

    candidate_str = str(path_like)
    wsl_path = _windows_to_wsl(candidate_str)
    if wsl_path is not None:
        return wsl_path

    path = Path(path_like)
    if base_dir and not path.is_absolute():
        path = Path(base_dir) / path
    return path


def _windows_to_wsl(candidate: str) -> Path | None:
    """
    Convert absolute Windows paths to their /mnt/<drive>/... counterpart when running in WSL.
    """
    if os.name != "posix" or "WSL_DISTRO_NAME" not in os.environ:
        return None
    if len(candidate) < 3 or candidate[1] != ":" or candidate[2] not in ("\\", "/"):
        return None

    try:
        win_path = PureWindowsPath(candidate)
    except Exception:
        return None

    drive = win_path.drive.rstrip(":\\/")
    if not drive:
        return None

    translated = Path("/mnt") / drive.lower()
    for part in win_path.parts[1:]:
        translated /= part
    return translated
