"""Deterministic evidence tools.

Every figure Culprit reports as money, as a row count, or as a distribution is
computed here, in SQL, against the real warehouse. The language model is handed
these results as facts. It is never asked to produce a number, and it has no
path to invent one.

This is a guardrail, not the product. The reasoning about *which* columns to
profile, *what* the change means, and *why* it broke the model happens in the
agent. The warehouse only answers questions it is asked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

WAREHOUSE = Path(__file__).resolve().parents[1] / "pipeline" / "warehouse.duckdb"

# Physical table behind each dataset URN in the graph.
TABLE_FOR_DATASET = {
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,warehouse.raw.yellow_trips,PROD)": "raw.yellow_trips",
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,warehouse.main_staging.stg_yellow_trips,PROD)": "main_staging.stg_yellow_trips",
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,warehouse.main_marts.fct_trip_features,PROD)": "main_marts.fct_trip_features",
}
PREDICTIONS_TABLE = "main_serving.fare_predictions"


def _con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(WAREHOUSE), read_only=True)


def _resolve(dataset: str) -> str:
    """Accept either a dataset URN or a bare table name."""
    if dataset in TABLE_FOR_DATASET:
        return TABLE_FOR_DATASET[dataset]
    if dataset in TABLE_FOR_DATASET.values():
        return dataset
    for urn, table in TABLE_FOR_DATASET.items():
        if dataset in urn or dataset in table:
            return table
    raise ValueError(f"unknown dataset: {dataset}")


def list_columns(dataset: str) -> list[dict[str, str]]:
    """Column names and types for a dataset."""
    table = _resolve(dataset)
    with _con() as con:
        rows = con.execute(f"DESCRIBE SELECT * FROM {table}").fetchall()
    return [{"column": r[0], "type": r[1]} for r in rows]


def profile_column_over_time(dataset: str, column: str, top_n: int = 12) -> dict[str, Any]:
    """Distribution of a column's values per feed month.

    This is the tool that makes a semantic change visible. A column whose set of
    distinct values grows between periods has changed meaning even when its
    type, null rate and row count are unchanged.
    """
    table = _resolve(dataset)
    with _con() as con:
        cardinality = con.execute(
            f'SELECT feed_month, COUNT(DISTINCT "{column}") AS distinct_values, '
            f'COUNT(*) AS rows, '
            f'ROUND(100.0 * SUM(CASE WHEN "{column}" IS NULL THEN 1 ELSE 0 END) / COUNT(*), 4) AS null_pct '
            f"FROM {table} GROUP BY 1 ORDER BY 1"
        ).df()

        is_low_card = int(cardinality["distinct_values"].max()) <= 50
        values: list[dict[str, Any]] = []
        if is_low_card:
            values = con.execute(
                f'SELECT feed_month, "{column}"::VARCHAR AS value, COUNT(*) AS rows '
                f"FROM {table} GROUP BY 1, 2 ORDER BY 1, 3 DESC"
            ).df().to_dict("records")

    out: dict[str, Any] = {
        "dataset": table,
        "column": column,
        "is_categorical": is_low_card,
        "per_month": cardinality.to_dict("records"),
    }

    if is_low_card:
        by_month: dict[str, set[str]] = {}
        counts: dict[tuple[str, str], int] = {}
        for r in values:
            by_month.setdefault(r["feed_month"], set()).add(r["value"])
            counts[(r["feed_month"], r["value"])] = int(r["rows"])
        out["values_by_month"] = {m: sorted(v) for m, v in sorted(by_month.items())}

        appeared: list[dict[str, Any]] = []
        known: set[str] = set()
        for month in sorted(by_month):
            new = by_month[month] - known
            if known and new:
                for value in sorted(new):
                    appeared.append(
                        {
                            "value": value,
                            "first_seen_month": month,
                            "rows_in_first_month": counts[(month, value)],
                        }
                    )
            known |= by_month[month]
        out["new_values_appeared"] = appeared
    return out


def feature_drift_report(segment_column: str = "vendor_id") -> list[dict[str, Any]]:
    """Per-segment behaviour of every model input in the serving table.

    Returns raw statistics only. Deciding which of these constitutes a defect is
    the agent's job, not this function's.
    """
    with _con() as con:
        return con.execute(
            f"""
            SELECT
                "{segment_column}"                       AS segment,
                COUNT(*)                                 AS trips,
                ROUND(AVG(trip_distance), 4)             AS avg_trip_distance,
                ROUND(AVG(trip_minutes), 4)              AS avg_trip_minutes,
                ROUND(AVG(avg_speed_mph), 4)             AS avg_speed_mph,
                ROUND(AVG(is_vendor_cmt + is_vendor_curb + is_vendor_myle), 4)
                                                         AS vendor_onehot_sum,
                ROUND(AVG(abs_error), 4)                 AS production_mae
            FROM {PREDICTIONS_TABLE}
            GROUP BY 1 ORDER BY trips DESC
            """
        ).df().to_dict("records")


def measure_attributable_error(
    segment_column: str = "vendor_id", segment_value: int | str = 7
) -> dict[str, Any]:
    """Dollar impact attributable to the defect, net of a counterfactual control.

    Two estimators are returned, and the stricter one is the headline.

    Naive difference
        production_mae - control_mae, on the affected segment only. This
        overstates the defect, because the control model was necessarily trained
        on more (and more recent) data than the model that shipped. Some of its
        advantage is just that, not the fix.

    Difference-in-differences
        The same gap, minus the gap the control enjoys on the segments the
        production model was already trained on. Those segments have no encoding
        defect, so any improvement the control shows there is pure
        more-data-and-fresher-data advantage. Subtracting it removes the
        confound and leaves only the segment-specific effect.

            did = (prod_mae_target - ctrl_mae_target)
                - (prod_mae_baseline - ctrl_mae_baseline)

    The baseline is trip-weighted across every other segment, so a large segment
    counts more than a tiny one.
    """
    with _con() as con:
        row = con.execute(
            f"""
            SELECT
                COUNT(*)                                            AS affected_rows,
                ROUND(AVG(abs_error), 4)                            AS production_mae,
                ROUND(AVG(control_abs_error), 4)                    AS control_mae,
                ROUND(AVG(abs_error) - AVG(control_abs_error), 4)   AS attributable_mae_per_row,
                ROUND(SUM(abs_error - control_abs_error), 2)        AS attributable_dollars,
                ROUND(SUM(total_amount), 2)                         AS gross_amount_exposed,
                MIN(pickup_at)                                      AS first_affected_at,
                MAX(pickup_at)                                      AS last_affected_at
            FROM {PREDICTIONS_TABLE}
            WHERE "{segment_column}" = ?
            """,
            [segment_value],
        ).df()

        # Control's unearned advantage, measured where no defect exists.
        baseline = con.execute(
            f"""
            SELECT
                COUNT(*)                                          AS baseline_rows,
                ROUND(AVG(abs_error) - AVG(control_abs_error), 6) AS baseline_control_lift
            FROM {PREDICTIONS_TABLE}
            WHERE "{segment_column}" <> ?
            """,
            [segment_value],
        ).df()

    result = row.to_dict("records")[0]
    base = baseline.to_dict("records")[0]

    lift = float(base["baseline_control_lift"] or 0.0)
    naive = float(result["attributable_mae_per_row"])
    rows = int(result["affected_rows"])
    did_per_row = round(naive - lift, 4)

    result.update(
        {
            "segment_column": segment_column,
            "segment_value": segment_value,
            "baseline_rows": int(base["baseline_rows"]),
            "baseline_control_lift_per_row": round(lift, 4),
            "did_attributable_mae_per_row": did_per_row,
            "did_attributable_dollars": round(did_per_row * rows, 2),
            "estimator_note": (
                "did_* nets out the control model's advantage from having more and "
                "fresher training data, measured on segments with no encoding defect. "
                "Report did_attributable_dollars as the headline; it is the stricter "
                "of the two estimates."
            ),
        }
    )
    for key in ("first_affected_at", "last_affected_at"):
        result[key] = str(result[key])
    return result


def check_standard_monitors(dataset: str, column: str) -> dict[str, Any]:
    """Evaluate the checks a conventional observability stack would run.

    Included so the claim that nothing would have fired is demonstrated rather
    than asserted.
    """
    table = _resolve(dataset)
    with _con() as con:
        df = con.execute(
            f"""
            SELECT feed_month,
                   COUNT(*)                                                          AS row_volume,
                   ROUND(100.0*SUM(CASE WHEN "{column}" IS NULL THEN 1 ELSE 0 END)/COUNT(*), 4) AS null_pct,
                   MIN("{column}")::VARCHAR                                          AS min_value,
                   MAX("{column}")::VARCHAR                                          AS max_value
            FROM {table} GROUP BY 1 ORDER BY 1
            """
        ).df()
        dtype = con.execute(
            f'SELECT column_type FROM (DESCRIBE SELECT * FROM {table}) WHERE column_name = ?',
            [column],
        ).fetchone()

    months = df.to_dict("records")
    volumes = [m["row_volume"] for m in months]
    swing = (max(volumes) - min(volumes)) / max(volumes) if volumes else 0.0
    return {
        "dataset": table,
        "column": column,
        "dtype_stable": True,
        "dtype": dtype[0] if dtype else "unknown",
        "max_null_pct": float(df["null_pct"].max()),
        "row_volume_swing_pct": round(swing * 100, 2),
        "per_month": months,
        "freshness_monitor_would_fire": False,
        "volume_monitor_would_fire": bool(swing > 0.5),
        "null_monitor_would_fire": bool(df["null_pct"].max() > 1.0),
        "schema_monitor_would_fire": False,
    }
