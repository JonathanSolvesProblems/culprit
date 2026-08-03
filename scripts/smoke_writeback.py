"""Smoke-test every write-back path against the live DataHub instance.

Needs no LLM key. Run this before spending a real investigation on write-back,
because a mutation that fails on stage is worse than one that fails here.

Uses a synthetic root cause clearly marked as a smoke test, then reads the
artifacts back out of DataHub to prove they actually landed rather than
trusting the mutation's return value.

Usage:  python scripts/smoke_writeback.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from culprit.mcp_bridge import DataHubMCP  # noqa: E402
from culprit.writeback import write_back_all  # noqa: E402

GMS = "http://localhost:8080"
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:duckdb,nyc_fare_predictor,PROD)"
# The dbt node, not the duckdb sibling: only the dbt node carries schemaMetadata,
# so only it can hold a column-level description.
DATASET = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,nyc_fares.warehouse.raw.yellow_trips,PROD)"
)

SYNTHETIC = {
    "confident": True,
    "headline": "[SMOKE TEST] vendor_id gained an unmapped value, corrupting fare model inputs",
    "root_cause_dataset": "raw.yellow_trips",
    "root_cause_column": "vendor_id",
    "change_description": "[SMOKE TEST] A new value 7 first appears in 2024-12.",
    "mechanism": "[SMOKE TEST] new code -> unmapped one-hot -> model error.",
    "affected_features": ["is_vendor_cmt", "is_vendor_curb", "is_vendor_myle", "avg_speed_mph"],
    "why_monitors_missed_it": "[SMOKE TEST] type, null rate and volume unchanged.",
    "impact": {
        "affected_rows": 66146,
        "attributable_dollars": 95158.12,
        "did_attributable_dollars": 90322.36,
        "attributable_mae_per_row": 1.4386,
        "did_attributable_mae_per_row": 1.3655,
    },
    "recommended_fix": "[SMOKE TEST] Add the missing vendor to the encoder and retrain.",
    "proven": ["[SMOKE TEST] proven claim"],
    "inferred": ["[SMOKE TEST] inferred claim"],
}


def gql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        f"{GMS}/api/graphql",
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


ORIGINAL_VENDOR_ID_DESC = (
    "Code identifying the TPEP provider that supplied the record. "
    "1 = Creative Mobile Technologies, 2 = Curb / VeriFone, 6 = Myle. "
    "NOTE: this description is the one the data team wrote when the pipeline was "
    "authored. It is the stated meaning of the column."
)


def clean() -> int:
    """Remove smoke-test artifacts so they do not appear alongside real ones."""
    removed = 0

    data = gql(
        """
        query($urn: String!) {
          dataset(urn: $urn) { incidents(start:0, count:50) {
            incidents { urn title status { state } } } }
        }
        """,
        {"urn": DATASET},
    )
    incidents = (
        ((data.get("data") or {}).get("dataset") or {}).get("incidents") or {}
    ).get("incidents") or []
    for inc in incidents:
        if "[SMOKE TEST]" not in (inc.get("title") or ""):
            continue
        gql(
            """
            mutation($urn: String!) {
              updateIncidentStatus(urn: $urn, input: {state: RESOLVED,
                message: "Smoke-test artifact, resolved automatically."})
            }
            """,
            {"urn": inc["urn"]},
        )
        print(f"  resolved incident {inc['urn']}")
        removed += 1

    docs = gql(
        '{searchAcrossEntities(input:{query:"*",types:[DOCUMENT],start:0,count:50})'
        "{searchResults{entity{urn ... on Document{info{title}}}}}}"
    )
    for r in (docs.get("data") or {}).get("searchAcrossEntities", {}).get(
        "searchResults", []
    ):
        urn = r["entity"]["urn"]
        title = ((r["entity"].get("info") or {}).get("title")) or ""
        if not title.startswith("Root cause:"):
            continue
        gql("mutation($urn: String!) { deleteDocument(urn: $urn) }", {"urn": urn})
        print(f"  deleted document {title!r}")
        removed += 1

    mcp = DataHubMCP()
    try:
        mcp.start()
        mcp.call(
            "update_description",
            {
                "entity_urn": DATASET,
                "column_path": "vendor_id",
                "operation": "replace",
                "description": ORIGINAL_VENDOR_ID_DESC,
            },
        )
        print("  restored vendor_id description")
        removed += 1
    finally:
        mcp.close()

    print(f"\ncleaned {removed} artifact(s)")
    return 0


def main() -> int:
    if "--clean" in sys.argv:
        return clean()

    mcp = DataHubMCP()
    failures = 0
    try:
        mcp.start()
        result = write_back_all(
            model_urn=MODEL,
            root_cause=SYNTHETIC,
            trace_markdown="| # | tool | args | ms |\n|---|---|---|---|\n| 1 | `smoke` | `{}` | 0 |",
            source_dataset_urn=DATASET,
            source_column="vendor_id",
            mcp=mcp,
        )

        print("=" * 72)
        print("WRITE-BACK RESULT")
        print("=" * 72)
        for key, value in result.items():
            status = "FAIL" if key.endswith("error") else "ok  "
            if key.endswith("error"):
                failures += 1
            print(f"  [{status}] {key}: {str(value)[:300]}")

        # Read the artifacts back. A mutation returning a URN is not proof it
        # rendered; querying it is.
        print("\n" + "=" * 72)
        print("VERIFY BY READING BACK FROM DATAHUB")
        print("=" * 72)

        incident_urn = result.get("incident_urn")
        if incident_urn:
            data = gql(
                """
                query($urn: String!) {
                  entity(urn: $urn) {
                    ... on Incident {
                      urn
                      incidentType
                      customType
                      title
                      priority
                      status { state }
                    }
                  }
                }
                """,
                {"urn": incident_urn},
            )
            print("  incident:", json.dumps(data.get("data", {}).get("entity"), indent=2))
            if data.get("errors"):
                print("  ERRORS:", data["errors"])
                failures += 1

        # Does the dataset now report incident health?
        data = gql(
            """
            query($urn: String!) {
              dataset(urn: $urn) {
                urn
                health { type status message }
                editableSchemaMetadata {
                  editableSchemaFieldInfo { fieldPath description }
                }
              }
            }
            """,
            {"urn": DATASET},
        )
        ds = (data.get("data") or {}).get("dataset") or {}
        print("  dataset health:", json.dumps(ds.get("health"), indent=2))
        fields = (ds.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []
        annotated = [f for f in fields if f.get("description")]
        print(f"  annotated columns: {[f['fieldPath'] for f in annotated]}")
        if not annotated:
            print("  FAIL: column annotation did not land")
            failures += 1

        print("\n" + "=" * 72)
        print("PASS" if failures == 0 else f"{failures} FAILURE(S)")
        print("=" * 72)
        if failures == 0:
            print(
                "\nClean up the smoke-test artifacts in the DataHub UI before recording"
                "\nthe demo, or they will appear alongside the real ones."
            )
        return 1 if failures else 0
    finally:
        mcp.close()


if __name__ == "__main__":
    sys.exit(main())
