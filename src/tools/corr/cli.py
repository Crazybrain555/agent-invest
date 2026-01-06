from __future__ import annotations

import argparse
from typing import List, Optional

from src.tools.corr.runner import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Factor correlation tool (Phase-0)")
    parser.add_argument("--config", type=str, default=None, help="Path to corr config YAML")
    parser.add_argument("--mode", type=str, default="many_to_many", help="pair | one_to_many | many_to_many")

    parser.add_argument("--group", action="append", help="Group selector (e.g. growth.profitability)")
    parser.add_argument("--factor", action="append", help="Factor spec (table::field or field)")
    parser.add_argument("--factor-a", type=str, help="Pair mode: factor A")
    parser.add_argument("--factor-b", type=str, help="Pair mode: factor B")
    parser.add_argument("--target", type=str, help="one_to_many: target factor")
    parser.add_argument("--candidates", type=str, help="one_to_many: candidate list")
    parser.add_argument("--tables", type=str, help="Restrict to tables (comma-separated)")
    parser.add_argument("--include-table", action="append", help="Append include table")
    parser.add_argument("--exclude-table", action="append", help="Append exclude table")

    parser.add_argument("--method", type=str, choices=["cross_sectional", "time_series"], help="Correlation method")
    parser.add_argument("--corr-type", type=str, choices=["pearson", "spearman"], help="Correlation type")
    parser.add_argument("--min-periods", type=int, help="Minimum periods per correlation")
    parser.add_argument("--threshold", type=float, help="High correlation threshold (abs)")
    parser.add_argument("--strict-name", action="store_true", help="Error on ambiguous factor name")

    parser.add_argument("--years", type=str, help="Fixed years, comma-separated")
    parser.add_argument("--start-date", type=str, help="Sampling start date (YYYYMMDD)")
    parser.add_argument("--end-date", type=str, help="Sampling end date (YYYYMMDD)")
    parser.add_argument("--random-days-per-year", type=int, help="Random days per year")
    parser.add_argument("--random-stocks-per-date", type=int, help="Random stocks per date")
    parser.add_argument("--random-seed", type=int, help="Random seed")

    parser.add_argument("--out-root", type=str, help="Override output root")
    parser.add_argument("--no-forbid-pool", action="store_true", help="Disable forbid pool exclusion")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache write")
    parser.add_argument("--no-progress", action="store_true", help="Disable tqdm progress bars")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
