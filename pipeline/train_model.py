"""Train the NYC fare predictor on real trip features.

Two variants get trained, and the difference between them is what makes the
headline dollar figure honest:

  production  the model that actually shipped. Trained on 2024-06 and 2024-09,
              months in which vendor 7 does not exist. Its encoder has no slot
              for a vendor it has never seen.

  control     the counterfactual. Same algorithm, same hyperparameters, but its
              encoder knows about vendor 7 and its training window includes
              vendor-7 trips.

Vendor-7 trips are intrinsically different (shorter, cheaper, better tipped), so
some prediction error on them is unavoidable and must NOT be billed to the
defect. Attributable error is the gap between the two models:

    attributable = MAE(production, vendor7) - MAE(control, vendor7)

That is the error the fix actually recovers. It is the only number Culprit
reports as money.

Usage:  python pipeline/train_model.py
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

HERE = Path(__file__).resolve().parent
WAREHOUSE = HERE / "warehouse.duckdb"
ARTIFACTS = HERE / "artifacts"

TRAIN_MONTHS = ("2024-06", "2024-09")
CONTROL_MONTHS = ("2024-06", "2024-09", "2025-03")  # control is allowed to see vendor 7

BASE_FEATURES = [
    "trip_distance",
    "trip_minutes",
    "avg_speed_mph",
    "passenger_count",
    "pickup_hour",
    "pickup_dow",
    "pu_location_id",
    "do_location_id",
    "is_vendor_cmt",
    "is_vendor_curb",
    "is_vendor_myle",
    "is_airport_rate",
    "is_card_payment",
]
TARGET = "total_amount"

HYPERPARAMS = dict(max_iter=250, learning_rate=0.1, max_depth=8, random_state=42)


def load(con: duckdb.DuckDBPyConnection, months: tuple[str, ...]) -> pd.DataFrame:
    """Every trip in the training months. No sampling anywhere in this project."""
    placeholders = ", ".join("?" for _ in months)
    return con.execute(
        f"SELECT * FROM main_marts.fct_trip_features WHERE feed_month IN ({placeholders})",
        list(months),
    ).df()


def train(df: pd.DataFrame, features: list[str]) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(**HYPERPARAMS)
    model.fit(df[features].to_numpy(dtype=np.float64), df[TARGET].to_numpy(dtype=np.float64))
    return model


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE), read_only=True)

    # ---------- production model ----------
    prod_df = load(con, TRAIN_MONTHS)
    vendors_seen = sorted(int(v) for v in prod_df["vendor_id"].dropna().unique())
    print(f"production training rows : {len(prod_df):,}")
    print(f"production vendors seen  : {vendors_seen}")
    prod_model = train(prod_df, BASE_FEATURES)
    prod_mae = mean_absolute_error(
        prod_df[TARGET], prod_model.predict(prod_df[BASE_FEATURES].to_numpy(dtype=np.float64))
    )
    print(f"production in-sample MAE : ${prod_mae:,.4f}")

    # ---------- control model ----------
    ctrl_df = load(con, CONTROL_MONTHS).copy()
    ctrl_df["is_vendor_helix"] = (ctrl_df["vendor_id"] == 7).astype(int)
    ctrl_features = BASE_FEATURES + ["is_vendor_helix"]
    print(f"\ncontrol training rows    : {len(ctrl_df):,}")
    print(f"control vendors seen     : {sorted(int(v) for v in ctrl_df['vendor_id'].dropna().unique())}")
    ctrl_model = train(ctrl_df, ctrl_features)
    ctrl_mae = mean_absolute_error(
        ctrl_df[TARGET], ctrl_model.predict(ctrl_df[ctrl_features].to_numpy(dtype=np.float64))
    )
    print(f"control in-sample MAE    : ${ctrl_mae:,.4f}")

    for name, model, feats, months, mae in (
        ("production", prod_model, BASE_FEATURES, TRAIN_MONTHS, prod_mae),
        ("control", ctrl_model, ctrl_features, CONTROL_MONTHS, ctrl_mae),
    ):
        with open(ARTIFACTS / f"{name}_model.pkl", "wb") as fh:
            pickle.dump({"model": model, "features": feats}, fh)
        (ARTIFACTS / f"{name}_meta.json").write_text(
            json.dumps(
                {
                    "variant": name,
                    "features": feats,
                    "training_months": list(months),
                    "training_rows": int(len(prod_df if name == "production" else ctrl_df)),
                    "vendors_in_training_data": vendors_seen
                    if name == "production"
                    else sorted(int(v) for v in ctrl_df["vendor_id"].dropna().unique()),
                    "hyperparameters": HYPERPARAMS,
                    "in_sample_mae": round(float(mae), 6),
                    "algorithm": "sklearn.HistGradientBoostingRegressor",
                    "target": TARGET,
                },
                indent=2,
            )
        )
    con.close()
    print(f"\nartifacts written to {ARTIFACTS}")


if __name__ == "__main__":
    main()
