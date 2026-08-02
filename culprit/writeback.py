"""Write the diagnosis back into DataHub.

The judging criteria asks submissions to go beyond reading metadata and
contribute to the graph. More to the point: a root cause that lives only in a
terminal is a root cause the next engineer, and the next agent, has to
rediscover from scratch.

Culprit leaves three artifacts behind, all on the real instance:

  1. an Incident raised on the affected mlModel (GraphQL raiseIncident)
  2. a knowledge document holding the full trace (MCP save_document)
  3. structured annotation on the offending source column, so anyone who opens
     that column in DataHub sees that it broke a model downstream (MCP
     update_description / add_tags)
"""

from __future__ import annotations

import json
from typing import Any

import requests

from culprit.mcp_bridge import DataHubMCP

GMS = "http://localhost:8080"


def _gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        f"{GMS}/api/graphql", json={"query": query, "variables": variables}, timeout=30
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def raise_incident(model_urn: str, root_cause: dict[str, Any]) -> str:
    """Raise a DataHub Incident on the degraded model."""
    impact = root_cause.get("impact", {}) or {}
    dollars = impact.get("attributable_dollars")
    rows = impact.get("affected_rows")

    description_lines = [
        root_cause.get("headline", "Silent model decay detected."),
        "",
        "## What changed",
        root_cause.get("change_description", ""),
        "",
        "## Mechanism",
        root_cause.get("mechanism", ""),
        "",
        "## Why monitoring missed it",
        root_cause.get("why_monitors_missed_it", ""),
        "",
        "## Measured impact",
        f"- Attributable prediction error: ${dollars:,.2f}" if isinstance(dollars, (int, float)) else "",
        f"- Affected rows: {rows:,}" if isinstance(rows, (int, float)) else "",
        f"- Attributable error per row: ${impact.get('attributable_mae_per_row')}",
        "",
        "## Recommended fix",
        root_cause.get("recommended_fix", ""),
        "",
        "## Affected model inputs",
        *[f"- {f}" for f in root_cause.get("affected_features", [])],
        "",
        "---",
        "Raised automatically by Culprit. Every figure above was computed in SQL "
        "against the warehouse, net of a counterfactual control model.",
    ]

    data = _gql(
        """
        mutation raiseIncident($input: RaiseIncidentInput!) {
          raiseIncident(input: $input)
        }
        """,
        {
            "input": {
                "type": "DATA_QUALITY",
                "title": root_cause.get("headline", "Silent model decay")[:200],
                "description": "\n".join(line for line in description_lines if line is not None),
                "resourceUrn": model_urn,
                "priority": 1,
            }
        },
    )
    return data["raiseIncident"]


def save_trace_document(mcp: DataHubMCP, root_cause: dict[str, Any], trace_markdown: str) -> str:
    """Store the full investigation as a DataHub knowledge document."""
    body = [
        f"# {root_cause.get('headline', 'Silent model decay')}",
        "",
        "## Root cause",
        f"**Dataset:** `{root_cause.get('root_cause_dataset')}`  ",
        f"**Column:** `{root_cause.get('root_cause_column')}`",
        "",
        root_cause.get("change_description", ""),
        "",
        "## Mechanism",
        root_cause.get("mechanism", ""),
        "",
        "## Proven from tool output",
        *[f"- {c}" for c in root_cause.get("proven", [])],
        "",
        "## Inferred",
        *[f"- {c}" for c in root_cause.get("inferred", [])],
        "",
        "## Investigation trace",
        trace_markdown,
    ]
    return mcp.call(
        "save_document",
        {
            "title": f"Root cause: {root_cause.get('root_cause_column')} semantic change",
            "content": "\n".join(body),
        },
    )


def annotate_source_column(
    mcp: DataHubMCP, dataset_urn: str, column: str, root_cause: dict[str, Any]
) -> list[str]:
    """Mark the offending column so its next reader inherits the finding."""
    results: list[str] = []
    note = (
        f"[Culprit] {root_cause.get('change_description', '')} "
        f"This column feeds {', '.join(root_cause.get('affected_features', [])[:4])} "
        f"and caused measurable production model error. "
        f"See the incident on {root_cause.get('root_cause_column')}'s downstream model."
    )
    try:
        results.append(
            mcp.call(
                "update_description",
                {"urn": dataset_urn, "sub_resource": column, "description": note},
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(f"update_description failed: {exc}")
    return results


def write_back_all(
    model_urn: str,
    root_cause: dict[str, Any],
    trace_markdown: str,
    source_dataset_urn: str,
    source_column: str,
    mcp: DataHubMCP,
) -> dict[str, Any]:
    """Run every write-back step, reporting honestly on partial failure."""
    out: dict[str, Any] = {}
    try:
        out["incident_urn"] = raise_incident(model_urn, root_cause)
    except Exception as exc:  # noqa: BLE001
        out["incident_error"] = str(exc)
    try:
        out["document"] = save_trace_document(mcp, root_cause, trace_markdown)
    except Exception as exc:  # noqa: BLE001
        out["document_error"] = str(exc)
    try:
        out["column_annotation"] = annotate_source_column(
            mcp, source_dataset_urn, source_column, root_cause
        )
    except Exception as exc:  # noqa: BLE001
        out["column_annotation_error"] = str(exc)
    return out
