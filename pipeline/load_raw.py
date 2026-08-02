"""Load real NYC TLC yellow-taxi parquet files into the DuckDB warehouse.

This is a real warehouse holding real published trip records. Nothing here is
synthetic and nothing is mocked.

Training window : months strictly BEFORE vendor 7 appears in the feed
Serving  window : months where vendor 7 is present and growing

Usage:  python pipeline/load_raw.py
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
WAREHOUSE = HERE / "warehouse.duckdb"

BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data"

# Real months. Vendor 7 (Helix) enters the real feed in 2024-12.
TRAIN_MONTHS = ["2024-06", "2024-09"]
SERVE_MONTHS = ["2024-12", "2025-03", "2025-06"]
ALL_MONTHS = TRAIN_MONTHS + SERVE_MONTHS

# Every month is loaded at its true published volume. Sampling happens later, at
# training time, and never in the warehouse.
#
# This matters for correctness, not just tidiness. Culprit claims that a
# conventional volume monitor would not have fired on this change. If training
# months were down-sampled here and serving months were not, that claim would be
# tested against an artefact of the loader rather than against the real feed.


def ensure(month: str) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / f"yellow_tripdata_{month}.parquet"
    if not dest.exists() or dest.stat().st_size == 0:
        print(f"  downloading {month} ...", flush=True)
        urllib.request.urlretrieve(f"{BASE}/yellow_tripdata_{month}.parquet", dest)
    return dest


def main() -> None:
    paths = {m: ensure(m) for m in ALL_MONTHS}

    if WAREHOUSE.exists():
        WAREHOUSE.unlink()
    con = duckdb.connect(str(WAREHOUSE))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")

    # Columns common to every month, named exactly as the TLC publishes them.
    cols = """
        "VendorID"::INTEGER              AS vendor_id,
        tpep_pickup_datetime             AS pickup_at,
        tpep_dropoff_datetime            AS dropoff_at,
        passenger_count::DOUBLE          AS passenger_count,
        trip_distance::DOUBLE            AS trip_distance,
        "RatecodeID"::DOUBLE             AS ratecode_id,
        store_and_fwd_flag               AS store_and_fwd_flag,
        "PULocationID"::INTEGER          AS pu_location_id,
        "DOLocationID"::INTEGER          AS do_location_id,
        payment_type::INTEGER            AS payment_type,
        fare_amount::DOUBLE              AS fare_amount,
        tip_amount::DOUBLE               AS tip_amount,
        total_amount::DOUBLE             AS total_amount
    """

    parts = [
        f"SELECT {cols}, '{month}' AS feed_month "
        f"FROM read_parquet('{paths[month].as_posix()}')"
        for month in ALL_MONTHS
    ]

    con.execute(
        "CREATE OR REPLACE TABLE raw.yellow_trips AS " + "\nUNION ALL\n".join(parts)
    )

    print("\nraw.yellow_trips loaded:")
    print(
        con.execute(
            """
            SELECT feed_month,
                   COUNT(*)                                            AS trips,
                   SUM(CASE WHEN vendor_id = 7 THEN 1 ELSE 0 END)      AS vendor7_trips
            FROM raw.yellow_trips GROUP BY 1 ORDER BY 1
            """
        ).df().to_string(index=False)
    )
    con.close()
    print(f"\nwarehouse: {WAREHOUSE}")


if __name__ == "__main__":
    main()
