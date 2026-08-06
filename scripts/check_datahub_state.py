"""Report what is currently in the DataHub instance.

Run before taking screenshots, so you know whether the incident, the document and
the column annotation are present and current.

Usage:  python scripts/check_datahub_state.py
"""

from __future__ import annotations

import json

import requests

GMS = "http://localhost:8080"
DS = "urn:li:dataset:(urn:li:dataPlatform:dbt,nyc_fares.warehouse.raw.yellow_trips,PROD)"
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:duckdb,nyc_fare_predictor,PROD)"


def gql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        f"{GMS}/api/graphql", json={"query": query, "variables": variables or {}}, timeout=30
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        print("  GraphQL errors:", payload["errors"][0].get("message", "")[:140])
    return payload.get("data") or {}


def main() -> None:
    print("=" * 70)
    print("DATAHUB STATE")
    print("=" * 70)

    d = gql(
        "query($urn:String!){mlModel(urn:$urn){name properties{"
        "mlFeatures customProperties{key value}}}}",
        {"urn": MODEL},
    )
    model = d.get("mlModel") or {}
    props = model.get("properties") or {}
    custom = {p["key"]: p["value"] for p in (props.get("customProperties") or [])}
    print(f"\nmlModel            : {model.get('name')}")
    print(f"  features         : {len(props.get('mlFeatures') or [])}")
    print(f"  trained on       : {custom.get('vendors_in_training_data')}")

    d = gql("query($urn:String!){dataset(urn:$urn){health{type status message}}}", {"urn": DS})
    print(f"\ndataset health     : {json.dumps((d.get('dataset') or {}).get('health'))}")

    d = gql(
        "query($urn:String!){dataset(urn:$urn){incidents(start:0,count:25){total "
        "incidents{urn title incidentType customType priority status{state}}}}}",
        {"urn": DS},
    )
    inc = ((d.get("dataset") or {}).get("incidents")) or {}
    active = [i for i in inc.get("incidents", []) if i["status"]["state"] == "ACTIVE"]
    print(f"\nincidents          : {inc.get('total')} total, {len(active)} active")
    for i in active:
        print(f"  ACTIVE {i['incidentType']}/{i.get('customType')}/{i['priority']}")
        print(f"         {i['title'][:80]}")
        print(f"         {i['urn']}")

    d = gql(
        "query($urn:String!){dataset(urn:$urn){editableSchemaMetadata{"
        "editableSchemaFieldInfo{fieldPath description}}}}",
        {"urn": DS},
    )
    esm = ((d.get("dataset") or {}).get("editableSchemaMetadata") or {}).get(
        "editableSchemaFieldInfo"
    ) or []
    annotated = [f for f in esm if f.get("description")]
    print(f"\nannotated columns  : {[f['fieldPath'] for f in annotated]}")
    for f in annotated:
        has_culprit = "[Culprit]" in (f.get("description") or "")
        print(f"  {f['fieldPath']}: Culprit note present = {has_culprit}")

    d = gql(
        '{searchAcrossEntities(input:{query:"*",types:[DOCUMENT],start:0,count:25})'
        "{total searchResults{entity{urn ... on Document{info{title}}}}}}"
    )
    sr = d.get("searchAcrossEntities") or {}
    print(f"\ndocuments          : {sr.get('total')}")
    for r in sr.get("searchResults", []):
        title = ((r["entity"].get("info") or {}).get("title")) or "(untitled)"
        print(f"  {title}")

    print("\n" + "=" * 70)
    print("UI: http://localhost:9002   (datahub / datahub)")
    print("=" * 70)


if __name__ == "__main__":
    main()
