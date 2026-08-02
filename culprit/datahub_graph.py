"""Read the DataHub context graph.

Culprit reaches DataHub two ways, deliberately:

  * DataHub's own MCP server (culprit/mcp_bridge.py) for the tools it exposes:
    search, entity fetch, lineage, schema fields, query history.

  * The GraphQL API here for the ML terminals. The MCP server's lineage tools
    are dataset-oriented, and the walk Culprit needs continues past the dataset
    boundary into mlFeature, mlFeatureTable, mlModel and the training run. That
    last stretch is the part no existing tool traverses, and it is the reason
    this project exists.
"""

from __future__ import annotations

from typing import Any

import requests

GMS = "http://localhost:8080"


def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.post(
        f"{GMS}/api/graphql",
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def _props_to_dict(props: list[dict[str, str]] | None) -> dict[str, str]:
    return {p["key"]: p["value"] for p in (props or [])}


def get_model_context(model_urn: str) -> dict[str, Any]:
    """Everything the graph knows about a production model.

    The custom properties carry the fact that matters most: which segment values
    were present in the data the model was trained on.
    """
    data = _gql(
        """
        query($urn: String!) {
          mlModel(urn: $urn) {
            urn
            name
            properties {
              name description version type mlFeatures
              customProperties { key value }
              hyperParams { name value }
              trainingMetrics { name value }
              groups { urn name }
            }
          }
        }
        """,
        {"urn": model_urn},
    )
    model = data["mlModel"]
    if not model:
        raise ValueError(f"model not found in DataHub: {model_urn}")
    props = model.get("properties") or {}
    return {
        "urn": model["urn"],
        "name": props.get("name"),
        "description": props.get("description"),
        "version": props.get("version"),
        "algorithm": props.get("type"),
        "features": props.get("mlFeatures") or [],
        "custom_properties": _props_to_dict(props.get("customProperties")),
        "hyper_params": {h["name"]: h["value"] for h in (props.get("hyperParams") or [])},
        "training_metrics": {
            m["name"]: m["value"] for m in (props.get("trainingMetrics") or [])
        },
        "model_group": (props.get("groups") or [{}])[0].get("urn"),
    }


def get_feature_context(feature_urn: str) -> dict[str, Any]:
    """A single model input, and the columns it is derived from."""
    data = _gql(
        """
        query($urn: String!) {
          mlFeature(urn: $urn) {
            urn
            name
            properties {
              description dataType
              customProperties { key value }
              sources { urn }
            }
          }
        }
        """,
        {"urn": feature_urn},
    )
    feature = data["mlFeature"]
    if not feature:
        raise ValueError(f"feature not found: {feature_urn}")
    props = feature.get("properties") or {}
    custom = _props_to_dict(props.get("customProperties"))
    return {
        "urn": feature["urn"],
        "name": feature.get("name"),
        "description": props.get("description"),
        "data_type": props.get("dataType"),
        "source_datasets": [s["urn"] for s in (props.get("sources") or [])],
        "source_column": custom.get("source_column"),
        "root_columns": [c for c in (custom.get("root_columns") or "").split(",") if c],
        "root_dataset": custom.get("root_dataset"),
    }


def get_upstream_lineage(dataset_urn: str, depth: int = 3) -> list[dict[str, Any]]:
    """Walk dataset lineage upstream. This lineage came from dbt build artifacts."""
    seen: set[str] = set()
    frontier = [dataset_urn]
    hops: list[dict[str, Any]] = []

    for level in range(depth):
        next_frontier: list[str] = []
        for urn in frontier:
            if urn in seen:
                continue
            seen.add(urn)
            data = _gql(
                """
                query($urn: String!) {
                  dataset(urn: $urn) {
                    urn
                    name
                    upstream: lineage(input: {direction: UPSTREAM, start: 0, count: 25}) {
                      relationships { entity { urn ... on Dataset { name } } }
                    }
                  }
                }
                """,
                {"urn": urn},
            )
            ds = data.get("dataset")
            if not ds:
                continue
            parents = [
                r["entity"]["urn"]
                for r in (ds.get("upstream", {}) or {}).get("relationships", [])
            ]
            hops.append({"hop": level, "dataset": urn, "name": ds.get("name"), "upstreams": parents})
            next_frontier.extend(parents)
        frontier = next_frontier
        if not frontier:
            break
    return hops


def find_production_models() -> list[dict[str, str]]:
    """Every mlModel in the graph. Culprit's starting point when no URN is given."""
    data = _gql(
        """
        query {
          search(input: {type: MLMODEL, query: "*", start: 0, count: 50}) {
            searchResults { entity { urn ... on MLModel { name } } }
          }
        }
        """
    )
    return [
        {"urn": r["entity"]["urn"], "name": r["entity"].get("name")}
        for r in data["search"]["searchResults"]
    ]
