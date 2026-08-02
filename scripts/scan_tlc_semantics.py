"""Scan real NYC TLC yellow-taxi files for genuine semantic changes over time.

The goal is to find a categorical column that gains a NEW value at a specific
month in the real published feed. If one exists, Culprit's demo fault is not
planted at all: it is a real change in a real public dataset.

Usage:  python scripts/scan_tlc_semantics.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import duckdb

BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data"
RAW = Path(__file__).resolve().parents[1] / "pipeline" / "raw"

# Columns whose *meaning* is carried by a small integer code. These are exactly
# the columns an encoder can silently mis-handle when a new code appears.
CODE_COLUMNS = ["payment_type", "RatecodeID", "VendorID", "trip_type", "store_and_fwd_flag"]

MONTHS = [
    "2022-06", "2022-12",
    "2023-03", "2023-06", "2023-09", "2023-12",
    "2024-03", "2024-06", "2024-09", "2024-12",
    "2025-03", "2025-06",
]


def download(month: str) -> Path | None:
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / f"yellow_tripdata_{month}.parquet"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = f"{BASE}/yellow_tripdata_{month}.parquet"
    try:
        print(f"  downloading {month} ...", flush=True)
        urllib.request.urlretrieve(url, dest)
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"  !! {month} unavailable: {exc}", flush=True)
        if dest.exists():
            dest.unlink()
        return None


def main() -> int:
    con = duckdb.connect()
    seen: dict[str, dict[str, set]] = {}

    for month in MONTHS:
        path = download(month)
        if path is None:
            continue
        cols = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))", [str(path)]
            ).fetchall()
        }
        seen[month] = {}
        for col in CODE_COLUMNS:
            if col not in cols:
                continue
            rows = con.execute(
                f'SELECT "{col}" AS v, COUNT(*) AS n FROM read_parquet(?) '
                f'GROUP BY 1 ORDER BY 2 DESC',
                [str(path)],
            ).fetchall()
            seen[month][col] = {(str(v), n) for v, n in rows}

    print("\n" + "=" * 78)
    print("VALUE SETS BY MONTH")
    print("=" * 78)
    for month in sorted(seen):
        print(f"\n--- {month} ---")
        for col, vals in seen[month].items():
            pretty = ", ".join(f"{v}={n:,}" for v, n in sorted(vals, key=lambda x: -x[1]))
            print(f"  {col:22s} {pretty}")

    print("\n" + "=" * 78)
    print("NEW CODES APPEARING (the semantic changes worth building on)")
    print("=" * 78)
    ordered = sorted(seen)
    for col in CODE_COLUMNS:
        known: set[str] = set()
        for month in ordered:
            vals = {v for v, _ in seen.get(month, {}).get(col, set())}
            if not vals:
                continue
            new = vals - known
            if known and new:
                counts = {v: n for v, n in seen[month][col]}
                detail = ", ".join(f"{v} ({counts[v]:,} rows)" for v in sorted(new))
                print(f"  {col}: NEW CODE {detail} first seen in {month}")
            known |= vals
    return 0


if __name__ == "__main__":
    sys.exit(main())
