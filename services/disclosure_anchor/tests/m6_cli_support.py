"""Read one CLI's own argument declaration, as the child process would.

Several M6 entry points build their parser inside ``main`` and expose no factory, so
there is nothing to import. Re-declaring the arguments in a test would re-create exactly
the producer/consumer drift the composition cases exist to catch, which is why the
namespace is taken at the real ``parse_args`` call instead.
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import patch


class _Parsed(Exception):
    """Raised once the real parser has produced its namespace; nothing else runs."""


def parse_cli_args(module: Any, argv: list[str]) -> argparse.Namespace:
    """Return what ``module.main(argv)`` would parse, without running anything after it."""
    captured: dict[str, argparse.Namespace] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def recording(self, args=None, namespace=None):  # type: ignore[no-untyped-def]
        captured["args"] = real_parse_args(self, args, namespace)
        raise _Parsed

    with patch.object(argparse.ArgumentParser, "parse_args", recording):
        try:
            module.main(argv)
        except _Parsed:
            pass
    if "args" not in captured:
        raise AssertionError(f"{module.__name__}.main did not parse the arguments it was given")
    return captured["args"]


__all__ = ["parse_cli_args"]
