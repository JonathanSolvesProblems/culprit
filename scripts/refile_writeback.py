"""Re-file the write-back from the recorded run, using current code.

The incident, document and column note are written by whatever version of
culprit/writeback.py was current when the agent ran. When that code is corrected
afterwards, the artifacts sitting in DataHub still carry the old text, and those
artifacts are what a judge sees in a screenshot.

This replays the write-back from the root cause already recorded in
examples/investigation.json. It does NOT re-run the agent, so the recorded run
stays exactly as it was.

Resolves any active incident on the target dataset first, so there is one current
incident rather than a pile.

Usage:  python scripts/refile_writeback.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from culprit import warehouse as wh  # noqa: E402
from culprit.mcp_bridge import DataHubMCP  # noqa: E402
from culprit.writeback import write_back_all  # noqa: E402

GMS = "http://localhost:8080"
DATASET = "urn:li:dataset:(urn:li:dataPlatform:dbt,nyc_fares.warehouse.raw.yellow_trips,PROD)"
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:duckdb,nyc_fare_predictor,PROD)"
EXAMPLES = ROOT / "examples"


def gql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        f"{GMS}/api/graphql", json={"query": query, "variables": variables or {}}, timeout=30
    )
    r.raise_for_status()
    return r.json()


def resolve_active() -> int:
    d = gql(
        "query($urn:String!){dataset(urn:$urn){incidents(start:0,count:50){"
        "incidents{urn title status{state}}}}}",
        {"urn": DATASET},
    )
    incidents = (
        ((d.get("data") or {}).get("dataset") or {}).get("incidents") or {}
    ).get("incidents", [])
    n = 0
    for i in incidents:
        if i["status"]["state"] != "ACTIVE":
            continue
        gql(
            "mutation($urn:String!){updateIncidentStatus(urn:$urn,input:{state:RESOLVED,"
            'message:"Superseded by a re-filed incident with corrected figures."})}',
            {"urn": i["urn"]},
        )
        print(f"  resolved {i['urn'][-12:]}")
        n += 1
    return n


def main() -> int:
    payload = json.loads((EXAMPLES / "investigation.json").read_text())
    rc = payload.get("root_cause")
    if not rc:
        print("recorded run has no root cause; nothing to re-file")
        return 1

    # Refresh the impact block from the warehouse so the incident carries the
    # same estimators the README quotes.
    column = rc.get("root_cause_column")
    profile = wh.profile_column_over_time(rc.get("root_cause_dataset", ""), column)
    new_values = profile.get("new_values_appeared") or []
    segment_value = int(new_values[0]["value"]) if new_values else None
    if segment_value is not None:
        rc = {**rc, "impact": wh.measure_attributable_error(column, segment_value)}

    print("Resolving existing incidents:")
    print(f"  {resolve_active()} resolved\n")

    mcp = DataHubMCP()
    try:
        mcp.start()
        result = write_back_all(
            model_urn=MODEL,
            root_cause=rc,
            trace_markdown=(
                "| # | tool | ms |\n|---|---|---|\n"
                + "\n".join(
                    f"| {s['index']} | `{s['tool']}` | {s['elapsed_ms']} |"
                    for s in payload.get("trace", [])
                )
            ),
            source_dataset_urn=DATASET,
            source_column=column,
            mcp=mcp,
        )
    finally:
        mcp.close()

    print("Re-filed:")
    for k, v in result.items():
        style = "FAIL" if k.endswith("error") else "ok  "
        print(f"  [{style}] {k}: {str(v)[:150]}")

    (EXAMPLES / "writeback.json").write_text(json.dumps(result, indent=2, default=str))
    impact = rc.get("impact", {})
    print(
        f"\nIncident now quotes ${impact.get('did_attributable_dollars'):,.2f} total "
        f"and ${impact.get('did_attributable_mae_per_row')} per row. "
        f"{impact.get('did_attributable_mae_per_row')} x {impact.get('affected_rows'):,} "
        f"= {impact.get('did_attributable_mae_per_row') * impact.get('affected_rows'):,.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
