"""Is the real VendorID=7 semantic change actually worth money?

Honest-number check, run BEFORE building the pipeline. If the excess error on
vendor-7 trips is negligible, the scenario changes. The number is never inflated
to fit the story.

Usage:  python scripts/validate_impact.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb

RAW = Path(__file__).resolve().parents[1] / "pipeline" / "raw"
TRAIN = RAW / "yellow_tripdata_2024-09.parquet"   # before vendor 7 exists
SERVE = RAW / "yellow_tripdata_2025-06.parquet"   # vendor 7 at 67k trips

con = duckdb.connect()

print("=" * 78)
print("1. Does vendor 7 exist in the training month?")
print("=" * 78)
print(
    con.execute(
        'SELECT "VendorID", COUNT(*) n FROM read_parquet(?) GROUP BY 1 ORDER BY 2 DESC',
        [str(TRAIN)],
    ).df().to_string(index=False)
)

print("\n" + "=" * 78)
print("2. Are vendor-7 trips materially DIFFERENT from vendor 1/2 trips?")
print("   (if they are identical, mis-encoding them costs nothing)")
print("=" * 78)
q = """
SELECT
    "VendorID"                              AS vendor,
    COUNT(*)                                AS trips,
    ROUND(AVG(trip_distance), 3)            AS avg_miles,
    ROUND(AVG(total_amount), 3)             AS avg_total,
    ROUND(AVG(fare_amount), 3)              AS avg_fare,
    ROUND(AVG(tip_amount), 3)               AS avg_tip,
    ROUND(AVG(total_amount / NULLIF(trip_distance, 0)), 3) AS avg_dollar_per_mile,
    ROUND(AVG(date_diff('minute', tpep_pickup_datetime, tpep_dropoff_datetime)), 2) AS avg_minutes
FROM read_parquet(?)
WHERE trip_distance BETWEEN 0.1 AND 100
  AND total_amount BETWEEN 1 AND 500
  AND fare_amount > 0
GROUP BY 1
ORDER BY trips DESC
"""
print(con.execute(q, [str(SERVE)]).df().to_string(index=False))

print("\n" + "=" * 78)
print("3. How much of the serving month is vendor 7, and what is it worth?")
print("=" * 78)
q2 = """
SELECT
    SUM(CASE WHEN "VendorID" = 7 THEN 1 ELSE 0 END)              AS vendor7_trips,
    COUNT(*)                                                      AS all_trips,
    ROUND(100.0 * SUM(CASE WHEN "VendorID" = 7 THEN 1 ELSE 0 END) / COUNT(*), 3) AS pct_of_feed,
    ROUND(SUM(CASE WHEN "VendorID" = 7 THEN total_amount ELSE 0 END), 2) AS vendor7_gross_dollars
FROM read_parquet(?)
WHERE trip_distance BETWEEN 0.1 AND 100
  AND total_amount BETWEEN 1 AND 500
  AND fare_amount > 0
"""
print(con.execute(q2, [str(SERVE)]).df().to_string(index=False))

print("\n" + "=" * 78)
print("4. Would ANY standard monitor fire on this change?")
print("=" * 78)
q3 = """
SELECT
    'row volume'   AS monitor, COUNT(*)::VARCHAR AS value FROM read_parquet(?)
UNION ALL SELECT 'null rate on VendorID',
    ROUND(100.0 * SUM(CASE WHEN "VendorID" IS NULL THEN 1 ELSE 0 END)/COUNT(*), 4)::VARCHAR || '%'
    FROM read_parquet(?)
UNION ALL SELECT 'VendorID dtype',
    (SELECT column_type FROM (DESCRIBE SELECT * FROM read_parquet(?)) WHERE column_name='VendorID')
UNION ALL SELECT 'distinct VendorID count',
    (SELECT COUNT(DISTINCT "VendorID")::VARCHAR FROM read_parquet(?))
"""
print(con.execute(q3, [str(SERVE)] * 4).df().to_string(index=False))
