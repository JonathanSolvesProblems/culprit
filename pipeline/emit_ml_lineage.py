"""Emit the ML half of the lineage graph into DataHub.

No DataHub sample datapack ships ML entities, so this is the work that makes
challenge #3 possible at all. The dataset half of the graph came from DataHub's
native dbt connector parsing real build artifacts. This script adds the ML
terminals on top of it:

    raw.yellow_trips.vendor_id
        -> stg_yellow_trips            (dbt, column-level lineage)
            -> fct_trip_features       (dbt, column-level lineage)
                -> mlFeature x13       (this script)
                    -> mlFeatureTable  (this script)
                        -> mlModel     (this script)
                            <- dataProcessInstance (training run, this script)

Every mlFeature records the exact source column it was derived from, which is
what lets Culprit walk from a degraded model back to a single raw column.

Usage:  python pipeline/emit_ml_lineage.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from datahub.emitter.mce_builder import (
    make_data_platform_urn,
    make_ml_feature_table_urn,
    make_ml_feature_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AuditStampClass,
    DataProcessInstanceInputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRunEventClass,
    DataProcessInstanceRunResultClass,
    DataProcessRunStatusClass,
    MLFeaturePropertiesClass,
    MLFeatureTablePropertiesClass,
    MLHyperParamClass,
    MLMetricClass,
    MLModelGroupPropertiesClass,
    MLModelPropertiesClass,
    MLTrainingRunPropertiesClass,
    RunResultTypeClass,
    VersionTagClass,
)

GMS = "http://localhost:8080"
HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "artifacts"

FEATURE_TABLE = "nyc_fare_features"
PLATFORM = "duckdb"
MODEL_ID = "nyc_fare_predictor"
MODEL_VERSION = "1.4.0"
RUN_ID = "nyc_fare_predictor_train_2024_10_01"

# Point at the dbt nodes rather than the target-platform siblings. DataHub's
# dbt connector puts schemaMetadata and column-level lineage on the dbt node,
# so these are the datasets that actually have columns to link a feature to.
FEATURES_DATASET = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "nyc_fares.warehouse.main_marts.fct_trip_features,PROD)"
)
RAW_DATASET = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,nyc_fares.warehouse.raw.yellow_trips,PROD)"
)

# feature name -> (dtype, source column in fct_trip_features, human description)
FEATURES: dict[str, tuple[str, str, str]] = {
    "trip_distance":    ("CONTINUOUS", "trip_distance",  "Metered trip distance in miles."),
    "trip_minutes":     ("CONTINUOUS", "trip_minutes",   "Trip duration derived from pickup and dropoff timestamps."),
    "avg_speed_mph":    ("CONTINUOUS", "avg_speed_mph",  "Average speed. Coalesced to 0 when duration is zero."),
    "passenger_count":  ("ORDINAL",    "passenger_count", "Reported passenger count."),
    "pickup_hour":      ("ORDINAL",    "pickup_hour",    "Hour of day the meter was engaged."),
    "pickup_dow":       ("ORDINAL",    "pickup_dow",     "Day of week the meter was engaged."),
    "pu_location_id":   ("NOMINAL",    "pu_location_id", "TLC pickup zone identifier."),
    "do_location_id":   ("NOMINAL",    "do_location_id", "TLC dropoff zone identifier."),
    "is_vendor_cmt":    ("BINARY",     "is_vendor_cmt",  "Trip supplied by vendor 1 (Creative Mobile Technologies)."),
    "is_vendor_curb":   ("BINARY",     "is_vendor_curb", "Trip supplied by vendor 2 (Curb / VeriFone)."),
    "is_vendor_myle":   ("BINARY",     "is_vendor_myle", "Trip supplied by vendor 6 (Myle)."),
    "is_airport_rate":  ("BINARY",     "is_airport_rate", "Trip used JFK or Newark rate code."),
    "is_card_payment":  ("BINARY",     "is_card_payment", "Trip was paid by card."),
}

# The raw columns each feature ultimately depends on. Recorded explicitly so the
# traversal has ground truth to check its column-level walk against.
ROOT_COLUMNS: dict[str, list[str]] = {
    "avg_speed_mph":  ["trip_distance", "pickup_at", "dropoff_at"],
    "trip_minutes":   ["pickup_at", "dropoff_at"],
    "is_vendor_cmt":  ["vendor_id"],
    "is_vendor_curb": ["vendor_id"],
    "is_vendor_myle": ["vendor_id"],
}


def main() -> None:
    emitter = DatahubRestEmitter(gms_server=GMS)
    meta = json.loads((ARTIFACTS / "production_meta.json").read_text())
    now_ms = int(time.time() * 1000)
    stamp = AuditStampClass(time=now_ms, actor="urn:li:corpuser:datahub")

    feature_urns: list[str] = []
    mcps: list[MetadataChangeProposalWrapper] = []

    # ---- mlFeature, one per model input ----------------------------------
    for name, (dtype, source_col, description) in FEATURES.items():
        urn = make_ml_feature_urn(FEATURE_TABLE, name)
        feature_urns.append(urn)
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=MLFeaturePropertiesClass(
                    description=description,
                    dataType=dtype,
                    sources=[FEATURES_DATASET],
                    customProperties={
                        "source_column": source_col,
                        "source_dataset": FEATURES_DATASET,
                        "root_columns": ",".join(ROOT_COLUMNS.get(name, [source_col])),
                        "root_dataset": RAW_DATASET,
                    },
                ),
            )
        )

    # ---- mlFeatureTable ---------------------------------------------------
    ft_urn = make_ml_feature_table_urn(PLATFORM, FEATURE_TABLE)
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=ft_urn,
            aspect=MLFeatureTablePropertiesClass(
                description=(
                    "Feature table backing the NYC fare predictor. Materialised by the "
                    "dbt model fct_trip_features."
                ),
                mlFeatures=feature_urns,
                customProperties={"source_dataset": FEATURES_DATASET},
            ),
        )
    )

    # ---- training run (dataProcessInstance) -------------------------------
    dpi_urn = f"urn:li:dataProcessInstance:{RUN_ID}"
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=dpi_urn,
            aspect=DataProcessInstancePropertiesClass(
                name=f"{MODEL_ID} training run",
                created=stamp,
                customProperties={
                    "training_months": ",".join(meta["training_months"]),
                    "training_rows": str(meta["training_rows"]),
                    "vendors_in_training_data": ",".join(
                        str(v) for v in meta["vendors_in_training_data"]
                    ),
                    "algorithm": meta["algorithm"],
                },
            ),
        )
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=dpi_urn,
            aspect=DataProcessInstanceInputClass(inputs=[FEATURES_DATASET]),
        )
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=dpi_urn,
            aspect=MLTrainingRunPropertiesClass(
                id=RUN_ID,
                trainingMetrics=[
                    MLMetricClass(name="in_sample_mae", value=str(meta["in_sample_mae"]))
                ],
                hyperParams=[
                    MLHyperParamClass(name=k, value=str(v))
                    for k, v in meta["hyperparameters"].items()
                ],
                outputUrls=[str(ARTIFACTS / "production_model.pkl")],
            ),
        )
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=dpi_urn,
            aspect=DataProcessInstanceRunEventClass(
                timestampMillis=now_ms,
                status=DataProcessRunStatusClass.COMPLETE,
                result=DataProcessInstanceRunResultClass(
                    type=RunResultTypeClass.SUCCESS, nativeResultType="sklearn"
                ),
            ),
        )
    )

    # ---- mlModelGroup and mlModel ----------------------------------------
    group_urn = f"urn:li:mlModelGroup:(urn:li:dataPlatform:{PLATFORM},{MODEL_ID},PROD)"
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=group_urn,
            aspect=MLModelGroupPropertiesClass(
                name=MODEL_ID,
                description="Upfront fare estimation for NYC yellow-taxi trips.",
            ),
        )
    )

    model_urn = f"urn:li:mlModel:(urn:li:dataPlatform:{PLATFORM},{MODEL_ID},PROD)"
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=model_urn,
            aspect=MLModelPropertiesClass(
                name=MODEL_ID,
                description=(
                    "Predicts total_amount for a yellow-taxi trip. Serves upfront fare "
                    "quotes. Retrained quarterly."
                ),
                version=VersionTagClass(versionTag=MODEL_VERSION),
                type=meta["algorithm"],
                groups=[group_urn],
                mlFeatures=feature_urns,
                trainingJobs=[dpi_urn],
                hyperParams=[
                    MLHyperParamClass(name=k, value=str(v))
                    for k, v in meta["hyperparameters"].items()
                ],
                trainingMetrics=[
                    MLMetricClass(name="in_sample_mae", value=str(meta["in_sample_mae"]))
                ],
                customProperties={
                    "training_months": ",".join(meta["training_months"]),
                    "vendors_in_training_data": ",".join(
                        str(v) for v in meta["vendors_in_training_data"]
                    ),
                    "serving_table": "main_serving.fare_predictions",
                    "feature_table": ft_urn,
                },
            ),
        )
    )

    for mcp in mcps:
        emitter.emit(mcp)
    emitter.flush()

    print(f"emitted {len(mcps)} aspects")
    print(f"  mlFeature        x{len(feature_urns)}")
    print(f"  mlFeatureTable    {ft_urn}")
    print(f"  dataProcessInstance {dpi_urn}")
    print(f"  mlModelGroup      {group_urn}")
    print(f"  mlModel           {model_urn}")


if __name__ == "__main__":
    main()
