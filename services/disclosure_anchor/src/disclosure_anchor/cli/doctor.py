"""CLI for doctor checks."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.doctor import render_report, run_doctor
from disclosure_anchor.settings import load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor doctor")
    parser.add_argument(
        "--full",
        action="store_true",
        help="rehash every registered raw document instead of the deterministic sample",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        metavar="N",
        help="number of first-by-path and latest documents to sample (default: 20)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.sample < 0:
        print("[FAIL] sample: must be >= 0", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(f"[FAIL] settings: {exc}", file=sys.stderr)
        return 2

    report = run_doctor(settings, full=args.full, sample_size=args.sample)
    print(render_report(report.results))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
